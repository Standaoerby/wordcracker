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
    _ngram_n_from_text,
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
    # R-27 WP1 meta (§10 scope) — «у вас есть Шекспир?» / «Шекспир
    # есть?»: presence-вопрос разрезолвился в АВТОРА, не в книгу. Старый
    # путь проваливался в _need_book → неавторитетный clarify → v4 LLM
    # planner ~40s → canned-фейл. Делегируем в author_metadata
    # (books_matched / sample_titles / total_downloads) — да/нет +
    # счётчики за секунды. Guard e.word: «у вас есть слово ardour» —
    # это про СЛОВО, авторский редирект не должен срабатывать.
    if e.author_regex and not e.word:
        from scripts.v2.planner.builders.author import _plan_author_metadata
        plan = _plan_author_metadata(e)
        if plan.steps:
            plan.render_notes = list(plan.render_notes or []) + [
                "PRESENCE-ВОПРОС: пользователь спрашивает, ЕСТЬ ли этот "
                "автор в корпусе. Первой строкой ответь явно: "
                "books_matched > 0 → «да, есть N книг» + 2-3 "
                "sample_titles + total_downloads; books_matched == 0 → "
                "честное «нет, в корпусе не нашёл».",
            ]
        return plan
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
    Copperfield». Phase 4 W-5 (2026-05-23): when the user names ≥2 books,
    fire `affinity_by_book` for EACH of them so the renderer can put
    signature words side-by-side in one table. Single-book queries keep
    the legacy single-step plan.

    Cap at 3 books to bound wall-clock — affinity_by_book is medium-cost
    and renderer can only meaningfully compare 2-3 in one answer.
    """
    if not e.book_id and not e.book_title:
        return _need_book(e)

    # Gather every book the extractor saw (primary + secondaries).
    # Mirror of `_plan_book_readability_compare` shape so multi-object
    # plans behave consistently across compare intents (W-5 acceptance).
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
    multi = total >= 2

    # Single-book legacy path (no multi-book fan-out needed).
    if not multi:
        if e.book_id:
            return QueryPlan(
                intent="book_compare", entities=e,
                steps=[PlanStep(tool="affinity_by_book",
                                args={"pg_id": e.book_id,
                                      "top": e.top_n or 30,
                                      "min_corpus_count": 500,
                                      "exclude_proper_nouns": True})],
                expected_cost="medium",
                explain=(f"affinity_by_book({e.book_id}) — single book; "
                         f"renderer asks user to name a peer to compare against"),
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
            explain="find_book → affinity_by_book (single); renderer asks for peer",
        )

    # Multi-book composite plan: one affinity_by_book per book (cap 3).
    steps: list[PlanStep] = []
    cap = 3
    for pg in book_ids[:cap]:
        idx = len(steps)
        steps.append(PlanStep(
            tool="affinity_by_book",
            args={"pg_id": pg,
                  "top": e.top_n or 30,
                  "min_corpus_count": 500,
                  "exclude_proper_nouns": True},
            optional=(idx > 0),
        ))
    for title in book_titles_unresolved[: cap - len(steps)]:
        idx = len(steps)
        steps.append(PlanStep(tool="find_book", args={"title": title}))
        steps.append(PlanStep(
            tool="affinity_by_book",
            args={"top": e.top_n or 30,
                  "min_corpus_count": 500,
                  "exclude_proper_nouns": True},
            depends_on=[idx], inject_result_as="pg_id",
            optional=True,
        ))
    notes = [
        "Это composite book_compare запрос — у пользователя несколько "
        "книг в одном вопросе. ОБЯЗАТЕЛЬНО покажи signature-слова "
        "ВСЕХ книг в ОДНОЙ таблице (колонки = книги, строки = слова) "
        "или в нескольких равноправных таблицах — без «вот первая, "
        "о второй спроси отдельно». Если для какой-то книги шаг упал — "
        "честно скажи об этом, не молчи.",
    ]
    return QueryPlan(
        intent="book_compare", entities=e,
        steps=steps,
        expected_cost="medium",
        explain=(f"affinity_by_book × {total} books — composite signature "
                 f"comparison (cap 3)"),
        render_notes=notes,
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
def _plan_book_top_words(e: Entities) -> QueryPlan:
    """R-29 S1 / bug A — honest book-scoped RAW-frequency builder.

    Mirror of `_plan_book_vocab`'s book-fallback mechanic, but routes to
    `top_ngrams_by_book` (most frequent words/n-grams IN the named book by
    raw count) — NOT `affinity_by_book` (corpus-relative signature words).
    Keeps the metric honest: frequency != affinity.

    Reached from `_plan_author_top_words` when the user named a book — the
    resolved book scope must never collapse into an author aggregate
    (S1 invariant «book НИКОГДА молча не → author»). `n` mirrors the
    author route's bigram/trigram bump via the shared `_ngram_n_from_text`.
    """
    n = _ngram_n_from_text((e.raw_misc or {}).get("raw_text") or "")
    top = e.top_n or 20
    if e.book_id:
        return QueryPlan(
            intent="book_top_words", entities=e,
            steps=[PlanStep(tool="top_ngrams_by_book",
                            args={"pg_id": e.book_id, "n": n, "top": top,
                                  "pos_filter": e.pos_filter})],
            expected_cost="medium",
            explain=(f"top_ngrams_by_book({e.book_id}, n={n}) — raw frequency "
                     f"in book (NOT affinity, NOT author aggregate)"),
        )
    if e.book_title:
        return QueryPlan(
            intent="book_top_words", entities=e,
            steps=[
                PlanStep(tool="find_book", args={"title": e.book_title}),
                PlanStep(tool="top_ngrams_by_book",
                         args={"n": n, "top": top,
                               "pos_filter": e.pos_filter},
                         depends_on=[0], inject_result_as="pg_id"),
            ],
            expected_cost="medium",
            explain=(f"find_book → top_ngrams_by_book для «{e.book_title}» "
                     f"(n={n}) — raw frequency in book"),
        )
    return _need_book(e)


@_with_copyright_check
def _plan_book_readability(e: Entities) -> QueryPlan:
    # Phase 5 W-5 (2026-05-24) — when the extractor surfaced ≥2 books,
    # the user wants comparison even when the intent classifier kept us
    # in single-book readability. «уровень сложности Dracula и
    # Frankenstein» phrasing has no explicit «сложнее ... или» marker
    # the book_readability_compare rules require, but the two-book
    # presence is itself a strong compare signal. Redirect so both books
    # actually surface in the answer (W-5 acceptance).
    multi_count = sum(1 for pg in e.multi_book_ids if pg) + sum(
        1 for t in e.multi_book_titles if t)
    if multi_count >= 1 and (e.book_id or e.book_title):
        return _plan_book_readability_compare(e)
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


# 2.7.35 (quality / book_archaic precision) — render honesty notes. The
# renderer is free-form LLM (rag_v2._llm_render) and SEES raw
# data.checked_book_vocab, so it paraphrased «проверено 4920 слов» (the
# coverage count read as text size) and occasionally invented word-rows
# (morrow/amidst) the tool never returned. These ride plan.render_notes →
# surfaced as priority [plan] instructions. See RAG_TASK WP-D / WP-E.
_ARCHAIC_RENDER_NOTES = [
    # WP-D — honest coverage caption.
    "ПОДПИСЬ ОХВАТА: data.checked_book_vocab — это число РАЗЛИЧНЫХ "
    "словоформ книги с частотой ≥2, сверенных со списком архаизмов, а НЕ "
    "размер текста и НЕ «слов в книге». Формулируй честно: «Проверено N "
    "различных словоформ (частота ≥2); найдено M архаизмов», где "
    "N=data.checked_book_vocab, M=data.seed_or_cache_hits. НЕ пиши "
    "«проверено N слов» в смысле объёма текста.",
    # WP-E(a) — no-invention guard (prompt side; deterministic row-drop is a
    # follow-up, RAG_TASK §6).
    "СТРОГО ПО ДАННЫМ: выведи РОВНО слова из data.top и не добавляй ни "
    "одного слова, которого там нет (частая ошибка — дописать по памяти "
    "morrow/amidst/amongst). Каждая строка таблицы обязана иметь "
    "соответствие в data.top по полю word; чего нет в data.top — того нет "
    "в ответе.",
    # WP-B provenance — let the renderer disclose the propn cleanup.
    "Если data.dropped_propn>0 — список уже очищен от data.dropped_propn "
    "имён собственных (топонимы/персонажи книги); можешь это упомянуть как "
    "признак точности, но не выдумывай конкретные отброшенные слова.",
]


@_with_copyright_check
def _plan_book_archaic(e: Entities) -> QueryPlan:
    if e.book_id:
        return QueryPlan(
            intent="book_archaic", entities=e,
            steps=[PlanStep(tool="book_archaic_words",
                            args={"pg_id": e.book_id, "top": e.top_n or 30})],
            expected_cost="medium",
            explain=f"book_archaic_words({e.book_id})",
            render_notes=list(_ARCHAIC_RENDER_NOTES),
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
            render_notes=list(_ARCHAIC_RENDER_NOTES),
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
    than silently returning Pliny / Roman Stoicism for «B2 без архаизмов».

    Phase 5 W-9 (2026-05-24) — stable disclosures for every declared
    filter that `top_books_by_downloads` cannot honor at the v1 layer:
    `level`, `country`, `year_from`/`year_to`. Before this, queries like
    «что почитать на B2 без архаизмов» extracted level=intermediate AND
    exclude_archaic=True but only the archaic side got disclosed —
    level was silently dropped and the answer showed plain top-by-
    downloads as if no level constraint was given. Per R-2 acceptance,
    each declared filter is either really applied or consistently
    disclosed; never silently ignored.
    """
    lang = e.lang_hint or "en"
    notes: list[str] = []
    # top_n: top_books_by_downloads accepts `top`, so the user's hint
    # is the one filter we can actually honor on this builder.
    top = e.top_n or 20
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
    if e.level:
        # CEFR filter not implemented at top_books_by_downloads — the
        # tool only knows downloads/lang. Without disclosure the user
        # gets «топ книг по downloads» and assumes B2 was respected.
        notes.append(
            f"Пользователь просил уровень {e.level!r}, но top_books_by_downloads "
            f"не имеет per-book CEFR-фильтра (BookProfile pipeline ещё не "
            f"онлайн). DISCLOSE честно: «отсортировал по популярности без "
            f"проверки CEFR — для оценки уровня каждой книги используй "
            f"book_readability (Flesch+CEFR за <2с)». НЕ замалчивай "
            f"ограничение, НЕ говори «отфильтровал по {e.level}»."
        )
    if e.country:
        # top_books_by_downloads honors `lang` (corpus language) but not
        # `country` (author origin). «британская классика» surfaces as
        # country=GB AND lang_hint=en — lang is propagated, country is
        # not — and the answer would silently ignore the country axis.
        notes.append(
            f"Пользователь указал страну ({e.country}), но top_books_by_downloads "
            f"фильтрует по языку корпуса (lang={lang}), не по country автора. "
            f"DISCLOSE: «выдача отсортирована по lang={lang} без отдельного "
            f"country-фильтра; для строгого country-среза используй "
            f"top_authors_by_country(country={e.country}) и далее "
            f"top_books_by_downloads(author_regex=...) per author». "
            f"НЕ замалчивай ограничение."
        )
    if e.year_from or e.year_to:
        notes.append(
            f"Пользователь сузил период ({e.year_from or '?'}-"
            f"{e.year_to or '?'}), но top_books_by_downloads сортирует по "
            f"downloads глобально (year-фильтр у этого тула не подключён). "
            f"DISCLOSE: «отсортировал по downloads без year-фильтра — для "
            f"строгого среза по периоду пройдись вручную по top списку и "
            f"отфильтруй по pub_year (есть в выдаче)»."
        )
    flagged_bits = []
    if e.exclude_archaic:
        flagged_bits.append("exclude_archaic")
    if e.level:
        flagged_bits.append(f"level={e.level}")
    if e.country:
        flagged_bits.append(f"country={e.country}")
    if e.year_from or e.year_to:
        flagged_bits.append(f"year={e.year_from or '?'}-{e.year_to or '?'}")
    flagged = (f" [disclosed unimplemented: {', '.join(flagged_bits)}]"
               if flagged_bits else "")
    return QueryPlan(
        intent="book_recommendation", entities=e,
        steps=[PlanStep(tool="top_books_by_downloads",
                        args={"top": top, "lang": lang})],
        expected_cost="medium",
        explain=(f"popular books (lang={lang}, top={top}) → user filters by "
                 f"readability{flagged}"),
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
        #
        # W-13 (Phase 5 P2, 2026-05-23) — explicit per_retriever=30 +
        # top=8 to cap wall-clock at the wrapper level. Was top=10 with
        # the wrapper-default per_retriever=60, which made «что почитать
        # после X» reliably 200-317s on cold cache. New budget targets
        # <60s on cold, <2s on warm.
        args={"topic": topic, "top": 8, "per_retriever": 30,
              "rerank_with": "bge_reranker"},
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
    long tail of «самая редкая» / «самая древняя» is deferred.

    W-11 (Phase 5 P2, 2026-05-23) — plural difficulty queries («какие
    книги XIX века самые сложные») now classify to this intent. There's
    still no per-period ranking-by-readability tool, but the smart-
    clarify recipe ships in <1s instead of LLM-fallback 50-60s parse-
    fail.
    """
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    raw_lc = raw.lower()
    if any(w in raw_lc for w in ("популярн", "скачиваем", "читаем",
                                  "popular", "downloaded", "most read")):
        # Honour period filter when present — top_books_by_downloads
        # accepts lang but not year (it's a global popularity proxy).
        # The render note tells the renderer to disclose the limit.
        notes = []
        if e.year_from or e.year_to:
            notes.append(
                f"Пользователь сузил период ({e.year_from or '?'}-"
                f"{e.year_to or '?'}). `top_books_by_downloads` сортирует "
                f"по downloads глобально (фильтр по году у этого тула не "
                f"включён). DISCLOSE: «отсортировал по downloads без "
                f"year-фильтра — для строгого среза по периоду пройдись "
                f"вручную по top списку и отфильтруй по pub_year»."
            )
        return QueryPlan(
            intent="book_extremum", entities=e,
            steps=[PlanStep(tool="top_books_by_downloads",
                            args={"top": 1})],
            expected_cost="medium",
            explain="book_extremum → top_books_by_downloads(top=1)",
            render_notes=notes,
        )
    # W-11 plural-difficulty case — «какие книги XIX века самые сложные»,
    # «самые простые романы у викторианцев», «hardest books of the 1800s».
    # No single tool ranks books by readability/archaic-density across a
    # period — BookProfile.archaic_density is in backlog (book_recommendation
    # B9 note). Emit a fast recipe-clarify so the user gets actionable
    # next steps in <1s instead of bouncing through LLM-fallback.
    difficulty_markers = (
        "сложн", "трудн", "архаичн", "устаревш", "прост", "лёгк", "легк",
        "complex", "difficult", "hard", "simplest", "easiest",
        "archaic", "simple",
    )
    plural_book_markers = (
        "какие", "самые", "наиболее", "the most", "hardest",
        "simplest", "easiest",
    )
    if (any(m in raw_lc for m in difficulty_markers)
            and any(m in raw_lc for m in plural_book_markers)):
        # Build a recipe that uses tools we already have.
        period_str = ""
        if e.year_from or e.year_to:
            period_str = (
                f"\n• Период извлечён: {e.year_from or '?'}-"
                f"{e.year_to or '?'}."
            )
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Ранжирование книг по сложности на уровне периода у меня "
                "нет одним вызовом (per-book readability считается, но "
                "не индексируется в виде «топ N сложных за XIX век»). "
                f"Recipe из 2 шагов:{period_str}\n"
                "1. Сначала возьми **топ-20 популярных книг** "
                "(`top_books_by_downloads`) — или сузь автором: "
                "«топ книг Dickens / Doyle / etc».\n"
                "2. Для каждой — спроси «уровень сложности X» "
                "(`book_readability`, Flesch + CEFR за <2с).\n"
                "3. Отсортируй вручную по Flesch (ниже = сложнее).\n\n"
                "Альтернатива — спроси «самая сложная книга у Dickens» "
                "(book-compare per-author уже работает), или «топ "
                "архаичных книг» через `book_archaic_words` per книгу."
            ),
            explain=("book_extremum → recipe-clarify (no single per-period "
                     "ranking-by-readability tool)"),
            authoritative_clarify=True,
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
        # R-27 WP1 fail-fast (§10 «самая длинная книга») — корпус-wide
        # length/rarity extremum требует derived-индекса (corpus_stats_
        # by_author знает longest_book только per-author). Этот clarify
        # с рабочими примерами — осознанный ответ; без флага он уходил
        # в v4 LLM planner на ~40s с тем же исходом.
        authoritative_clarify=True,
    )
