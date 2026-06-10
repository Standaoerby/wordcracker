"""Author-level plan builders.

`top_authors_books`, `author_metadata`, `author_lookup`, `author_top_words`,
`author_vocab`, `author_compare`, `author_closest`, `author_attribution`,
`author_influences`.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import (
    PlanStep,
    QueryPlan,
    _ambiguous_author_clarify,
    _auto_min_corpus_count,
    _need_author,
    _with_author_copyright_check,
    _with_copyright_check,
)
from scripts.v2.planner.builders.book import (
    _plan_book_lookup,
    _plan_book_vocab,
)


# Phase 5 W-5 (2026-05-24) — country tokens recognized for the
# author_compare → country_compare redirect. Mirrors the entity
# extractor's COUNTRY_ALIASES coverage but keyed by canonical 2-letter
# code so we can count distinct countries in one pass. Latin tokens
# require word boundaries (avoid «german» inside «germanic»); Cyrillic
# stems substring-match for natural RU morphology.
_COUNTRY_PROBE_TOKENS: dict[str, str] = {
    "британск": "GB", "english": "GB", "english-language": "GB",
    "english language": "GB", "english-speaking": "GB",
    "english speaking": "GB",
    "british": "GB", "англия": "GB", "english-language\\b": "GB",
    "uk": "GB", "брит": "GB", "английск": "GB", "gb": "GB",
    "американск": "US", "american": "US", "американ": "US",
    "usa": "US", "us": "US", "amer": "US",
    "русск": "RU", "russian": "RU", "russia": "RU",
    "французск": "FR", "french": "FR", "франция": "FR",
    "немецк": "DE", "german": "DE", "germany": "DE",
}


def _query_mentions_two_countries(text: str) -> bool:
    """W-5 (2026-05-24) — count distinct country codes named in the
    raw query text. Used by `_plan_author_compare` to detect a
    «compare two-countries» phrasing that the entity extractor
    surfaced only one country for (it returns the FIRST match, not
    the full set).

    Returns True iff ≥2 distinct country codes are mentioned.
    """
    if not text:
        return False
    import re as _re
    s = text.lower()
    seen: set[str] = set()
    for token, code in _COUNTRY_PROBE_TOKENS.items():
        # Cyrillic / non-ASCII stem — substring match (covers
        # «русск-ий / русск-ого / русск-ие»).
        if any(ord(ch) > 127 for ch in token):
            if token in s:
                seen.add(code)
            continue
        # Latin alias — require word boundary so «german» doesn't
        # match inside «germanic».
        if _re.search(rf"\b{_re.escape(token)}\b", s):
            seen.add(code)
        if len(seen) >= 2:
            return True
    return len(seen) >= 2


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
    # Phase 4 — fan-out invariant. Builder emits a SINGLE primary step
    # with `fan_out="author_regex"`; the router clones it per
    # `e.multi_author_regex[:3]` before executing. Closes E5
    # structurally: «X у автор-1 и автор-2» works the same way for
    # author_vocab / word_contexts / word_emotion / word_collocates /
    # word_pos / word_etymology — all rely on the same invariant, no
    # builder reimplements the loop.
    steps = [PlanStep(
        tool="affinity_by_author",
        args={"author_regex": e.author_regex,
              "top": e.top_n or 30,
              "min_corpus_count": min_cc,
              "pos_filter": e.pos_filter},
        fan_out="author_regex",
    )]
    explain = f"affinity_by_author({e.author_regex})"
    if e.multi_author_regex:
        explain += f" + fan-out [{len(e.multi_author_regex[:3])} more]"
    if propn_strict:
        explain += f" [propn_strict: min_corpus_count={min_cc}]"
    return QueryPlan(
        intent="author_vocab", entities=e, steps=steps,
        expected_cost="medium",
        explain=explain,
    )


@_with_author_copyright_check
def _plan_author_compare(e: Entities) -> QueryPlan:
    others = e.multi_author_regex
    if not e.author_regex or not others:
        # Phase 4 W-5 (2026-05-23) — «сравни X и Y» / «compare X and Y»
        # where X, Y are BOOK titles (not authors) used to bounce here
        # with the «Нужны два автора» clarify because no author_regex
        # matched. If the extractor surfaced ≥2 books, redirect to
        # `_plan_book_compare` which will fan out per book. Closes W-5
        # «сравни Dracula и Frankenstein» path.
        books_count = (1 if e.book_id or e.book_title else 0) + len(
            e.multi_book_ids or []) + sum(
            1 for t in (e.multi_book_titles or []) if t)
        if books_count >= 2:
            from scripts.v2.planner.builders.book import _plan_book_compare
            return _plan_book_compare(e)
        # Phase 5 W-5 (2026-05-24) — «compare British and American X»
        # surfaces as author_compare in the intent classifier when the
        # English-only country regex didn't catch (covered in
        # country_compare rule), but if a single country is extracted
        # AND the query carries a paired-country signal in the raw
        # text, redirect to country_compare instead of clarify-bounce.
        # The text-side check picks up the OTHER country adjective the
        # entity extractor didn't keep (we only carry one `country`
        # field today). Closes the W-5 «односторонний список» complaint
        # for compare queries that talk about 2 countries.
        if e.country and _query_mentions_two_countries(
                (e.raw_misc or {}).get("raw_text", "")):
            from scripts.v2.planner.builders.composite import _plan_country_compare
            return _plan_country_compare(e)
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


# S-R5b (2026-05-30) — author_lookup-scoped dominant resolve.
# «какие книги у Уэллса» asks to SEE the books; bouncing it to a 5-way
# clarify when one Wells (H.G., 88.9% of the surname's downloads / 9.4×
# over the runner-up Basil) obviously dominates is the wrong UX *for
# this intent*. The global v6 resolver still clarifies a bare «Уэллса»
# (E13 / TZ §W-1 line 51 — unchanged), and so do author_metadata /
# author_vocab; this lowered, intent-scoped threshold fires ONLY inside
# _plan_author_lookup. A genuinely close field (≈equal downloads) still
# clarifies — see negative guard test.
#
# Thresholds mirror the v6 dominant-homonym shape (share OR ratio) but
# lowered from the global 90% / 10× so Wells (88.9% / 9.4×, just under)
# resolves. Wells sits above BOTH lowered floors.
AUTHOR_LOOKUP_DOMINANCE_SHARE = 0.60
AUTHOR_LOOKUP_DOMINANCE_RATIO = 3.0


def _dominant_author_lookup_regex(e: Entities) -> str | None:
    """Return `^<re.escaped canonical>` of the dominant author when an
    ambiguous surname has a clear download leader, else None.

    Scoped to author_lookup. Operates on the already-collected
    `author_clarify_candidates` (each `{name, downloads, books}`,
    `downloads` = v6 prominence) — does NOT re-run the resolver, so the
    global clarify behaviour other intents rely on is untouched.

    Robust to candidate ordering: picks the max-downloads candidate
    rather than assuming index 0. The regex is `^` + re.escape(name) so
    a canonical carrying regex metachars («Wells, H. G. (Herbert
    George)») matches its own books via `_select_books`' contains() and
    does NOT leak into a sibling Wells.
    """
    import re as _re
    cands = e.author_clarify_candidates or []
    if len(cands) < 2:
        return None
    dls = [max(int(c.get("downloads", 0) or 0), 0) for c in cands]
    top_idx = max(range(len(cands)), key=lambda i: dls[i])
    top_dl = dls[top_idx]
    if top_dl <= 0:
        return None  # no popularity signal — can't call a dominant; clarify
    total = sum(dls)
    runner_dl = max(dl for i, dl in enumerate(dls) if i != top_idx)
    top_share = top_dl / total if total > 0 else 0.0
    ratio = (top_dl / runner_dl) if runner_dl > 0 else 999.0
    if (top_share >= AUTHOR_LOOKUP_DOMINANCE_SHARE
            or ratio >= AUTHOR_LOOKUP_DOMINANCE_RATIO):
        name = (cands[top_idx].get("name") or "").strip()
        if name:
            return "^" + _re.escape(name)
    return None


def _plan_author_lookup(e: Entities) -> QueryPlan:
    """«Какие книги у X» / «list books by X» — reuses author_metadata which
    already returns `sample_titles` (up to 10) + `books_matched` count.
    Renderer formats the list; the LLM prompt's strict-facts rule keeps
    it bounded to actual data."""
    if not e.author_regex:
        return _need_author(e)
    # S-R5b — for THIS intent, a clearly dominant homonym resolves to the
    # leader (and lists the books) instead of clarifying. Other intents
    # keep the bare-surname clarify (E13 / W-1 untouched).
    dominant = _dominant_author_lookup_regex(e)
    if dominant is not None:
        from dataclasses import replace
        e = replace(e, author_regex=dominant, author_clarify_candidates=[])
    else:
        ambiguous = _ambiguous_author_clarify(e)
        if ambiguous is not None:
            return ambiguous
    return QueryPlan(
        intent="author_lookup", entities=e,
        steps=[PlanStep(tool="author_metadata",
                        args={"author_regex": e.author_regex})],
        expected_cost="cheap",
        # S-R5b L2 (E1 render) — the live renderer is the LLM path
        # (_llm_render), which receives author_metadata's `data`
        # (incl. the download-ranked `sample_titles` from the S-R5 data
        # fix) but NOT a directive to enumerate them — so a resolved
        # «какие книги у Уэллса» could still come back as a bare bio
        # card. This plan-level render_note instructs the renderer to
        # list the books with their titles. It rides plan.render_notes
        # → _llm_render render_instructions (rag_v2), so it needs no
        # change to the fingerprinted author_metadata wrapper (no
        # fixture re-record). The deterministic AUTHOR_METADATA view is
        # not on the live answer path (template_executor.render_view has
        # no production caller), so it is intentionally left untouched.
        # S-R1 (R-27, 2026-06-10) — anti-fabrication clause. Prod
        # feedback: the LLM renderer decorated the sample_titles list
        # with invented per-book annotations (plots, genres, dates).
        # author_metadata carries ONLY titles + counters — any
        # description next to a title is fabricated. The note now
        # forbids it explicitly and redirects the «tell me more»
        # impulse into a follow-up suggestion (rule 7 of RENDER_PROMPT
        # already asks for next-step questions).
        render_notes=[
            "Это запрос «какие книги у автора» (author_lookup). Перечисли "
            "конкретные книги автора СПИСКОМ, взяв названия из "
            "tool_results[*].data.sample_titles (они уже отсортированы по "
            "downloads — самые каноничные сверху). Укажи общее число книг "
            "из books_matched. НЕ ограничивайся биокарточкой с годами жизни.",
            "НЕ ВЫДУМЫВАЙ описания книг. В data есть ТОЛЬКО названия "
            "(sample_titles) и счётчики (books_matched) — аннотаций, "
            "жанров, сюжетов и годов написания там НЕТ. Список = голые "
            "названия без описаний; любая приписка «роман о…» — "
            "галлюцинация. Если хочется рассказать о книге — предложи "
            "follow-up вопрос («расскажи про <название>»), а не сочиняй.",
        ],
        explain=f"author_lookup → author_metadata({e.author_regex}) "
                "for sample_titles + books_matched",
    )
