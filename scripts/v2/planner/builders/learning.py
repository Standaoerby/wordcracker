"""Learning / lexical / translation / export plan builders.

`learning`, `learning_books`, `lexical_wealth`, `vocab_passport`,
`translation_quality`, `translate_word_list`, `export_word_list`.

Phase 4 / T4 (REMEDIATION_BRIEF) — extracted from plan.py.
"""
from __future__ import annotations

import re

from scripts.v2.planner.entities import Entities
from scripts.v2.planner.builders._common import (
    PlanStep,
    QueryPlan,
    _need_author,
    _scope_from,
    _with_author_copyright_check,
    _with_copyright_check,
)


@_with_copyright_check
def _plan_learning(e: Entities) -> QueryPlan:
    scope = _scope_from(e)
    if scope == "all_corpus":
        # R-27 WP1 fail-fast (B106) — этот clarify ОСОЗНАННЫЙ (unscoped
        # word-list — низкоценный продуктовый ответ, нужен scope), а не
        # «rules-path сдался». Без authoritative-флага rag_v2 уводил
        # запрос в v4 LLM planner: 2 попытки × ~15s → canned «не
        # получилось разобрать» за ~40s. Теперь ответ за секунды, с
        # рабочими примерами. Книжные learning-запросы сюда больше не
        # попадают — их забирает learning_books (выше по priority).
        return QueryPlan(
            intent="clarify", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Для изучаемой лексики уточни: для какого автора или книги? "
                "Пример: «B1 vocab из Pride and Prejudice», «слова для Wodehouse». "
                "А если нужны КНИГИ под твой уровень — спроси «какие книги "
                "почитать для уровня B2»."
            ),
            explain="learning_words needs scope (authoritative — no v4 rescue)",
            authoritative_clarify=True,
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
    # Phase 3 W-9 (Stan 2026-05-22) — when user says «B2 без архаизмов»,
    # learning_words doesn't currently filter by archaic_density (no per-
    # word archaic tag at v1 level; only enrich_word LLM verdicts cached
    # in word_dictionary). Surface the limitation as a render_note so
    # the answer doesn't pretend to honor the filter while just returning
    # top-by-downloads / band-pass CEFR words.
    notes: list[str] = []
    if e.exclude_archaic:
        notes.append(
            "ПОЛЬЗОВАТЕЛЬ просил «без архаизмов», но learning_words v1 "
            "не имеет per-word archaic-density фильтра (только "
            "post-hoc от enrich_word LLM-кэша). "
            "DISCLOSE честно: «вернул CEFR-band пресет без отдельной "
            "фильтрации архаизмов; если в списке встречаются thee/thou/"
            "hath/ere/oft/yon — отметь явно». "
            "НЕ замалчивай это ограничение, НЕ говори «отфильтровал "
            "архаизмы»."
        )
    # R-27 WP1 Дополнение Б (Q20, тест-ран) — first-session «дай N слов
    # из книги X с переводами»: learning_words(book) + перевод списка
    # ОДНИМ заходом. Раньше запрос не матчился ни одним правилом → v4
    # LLM ~46s → 0 calls → ложное «упомяни книгу» (книга была названа).
    # Перевод — enrich_word fan-out по строкам результата learning_words
    # (router-injected word@<rank>, та же механика, что pg_id@<rank> в
    # learning_books). Кап 10 слов — бюджетный расчёт translate_word_list
    # (10 × ~1.5s Wiktionary + render < chat timeout); 1 + 10 = 11 ≤
    # tool_calls_max=12. Followup-путь «переведи эти слова»
    # (translate_word_list, _translate_to из history) не затронут —
    # триггер только на явное «с перевод*» в тексте текущего запроса.
    raw_text = (e.raw_misc or {}).get("raw_text", "") or ""
    wants_translation = bool(re.search(
        r"\bс\s+перевод\w*|\bwith\s+translations?\b", raw_text,
        re.IGNORECASE))
    if wants_translation:
        trans_top = min(eff_top, 10)
        args["top"] = trans_top
        if requested > trans_top:
            args["_capped_from"] = requested
        steps = [PlanStep(tool="learning_words", args=args)]
        for rank in range(trans_top):
            steps.append(PlanStep(
                tool="enrich_word",
                args={"target_lang": "ru"},
                depends_on=[0], inject_result_as=f"word@{rank}",
                optional=True,   # один Wiktionary-промах не валит план
            ))
        notes.append(
            "ПЕРЕВОДЫ: пользователь просил слова С ПЕРЕВОДАМИ. Сведи "
            "learning_words + enrich_word в ОДНУ таблицу «слово | "
            "перевод | определение» в порядке списка learning_words. "
            "Если enrich_word для какого-то слова упал — оставь ячейку "
            "перевода пустой и честно скажи об этом, НЕ выдумывай "
            "перевод."
        )
        return QueryPlan(
            intent="learning", entities=e,
            steps=steps,
            expected_cost="heavy",
            explain=(f"learning_words({scope}, top={trans_top}"
                     f"{f' [capped from {requested}]' if requested > trans_top else ''}) "
                     f"+ enrich_word × {trans_top} (word@rank, "
                     f"target_lang=ru) — слова с переводами одним заходом"),
            render_notes=notes,
        )
    return QueryPlan(
        intent="learning", entities=e,
        steps=[PlanStep(tool="learning_words", args=args)],
        expected_cost="medium",
        explain=(f"learning_words({scope}, level={e.level or 'intermediate'}, "
                 f"top={eff_top}{f' [capped from {requested}]' if requested > 30 else ''}"
                 f"{' [exclude_archaic flagged → render disclose]' if e.exclude_archaic else ''})"),
        render_notes=notes,
    )


# R-27 WP1 (B106) — размер пула кандидатов для learning_books. Жёстко
# ограничен RequestBudget.tool_calls_max=12: 1 шаг пула + POOL шагов
# book_readability = 11 ≤ 12. book_readability ~1-2s/книга (cacheable),
# холодный прогон укладывается в 45s-бюджет интента с запасом на рендер.
_LEARNING_BOOKS_POOL = 10

# Какие банды cefr_heuristic (v1 book_readability: A2 / B1 / B1-B2 /
# B2 / C1 / C2+) считаются подходящими для запрошенного уровня.
_CEFR_BANDS = {
    "A1": ("A2",),
    "A2": ("A2", "B1"),
    "B1": ("B1", "B1-B2"),
    "B2": ("B1-B2", "B2"),
    "C1": ("C1", "C2+"),
    "C2": ("C1", "C2+"),
}
_LEVEL_BANDS = {  # e.level (словесный, без CEFR-буквы) → банды
    "basic": ("A2", "B1"),
    "intermediate": ("B1", "B1-B2", "B2"),
    "advanced": ("C1", "C2+"),
}


def _plan_learning_books(e: Entities) -> QueryPlan:
    """R-27 WP1 (B106) — «какие книги почитать, если у меня уровень B2»,
    «я учу английский, с чего начать?».

    Фаза 0: предрассчитанной читабельности по корпусу НЕТ — только
    on-demand book_readability(pg_id) (~1-2s, cacheable). Поэтому план —
    композиция существующих тулов на ОГРАНИЧЕННОМ пуле кандидатов, без
    новых fingerprinted-врапперов и без скана 47К книг:

      s0: top_books_by_downloads(top=10) — пул кандидатов (cheap);
      s1..s10: book_readability per кандидат — pg_id роутер инжектит из
               строки rank пула (inject_result_as="pg_id@<rank>",
               optional — одна упавшая оценка не валит план);
      рендер: одна таблица, фильтр по CEFR-банду, top-5..7 рекомендаций.

    Дефолт без уровня = минимальный порог вхождения (max Flesch).
    """
    raw = (e.raw_misc or {}).get("raw_text", "") or ""
    m = re.search(r"\b([abc][12])\b", raw, re.IGNORECASE)
    cefr = m.group(1).upper() if m else None
    lang = e.lang_hint or "en"

    steps = [PlanStep(tool="top_books_by_downloads",
                      args={"top": _LEARNING_BOOKS_POOL, "lang": lang})]
    for rank in range(_LEARNING_BOOKS_POOL):
        steps.append(PlanStep(
            tool="book_readability", args={},
            depends_on=[0], inject_result_as=f"pg_id@{rank}",
            optional=True,
        ))

    if cefr and cefr in _CEFR_BANDS:
        target = (f"уровень {cefr}: подходящими считай книги с "
                  f"cefr_heuristic из {{{', '.join(_CEFR_BANDS[cefr])}}}")
    elif e.level in _LEVEL_BANDS:
        target = (f"уровень «{e.level}»: подходящими считай книги с "
                  f"cefr_heuristic из {{{', '.join(_LEVEL_BANDS[e.level])}}}")
    else:
        target = ("уровень не указан — дефолт «минимальный порог "
                  "вхождения»: рекомендуй книги с НАИБОЛЬШИМ "
                  "flesch_reading_ease (самые простые для старта)")

    notes = [
        "LEARNING_BOOKS: пользователь просит КНИГИ для изучения "
        f"английского. Целевой {target}.\n"
        "Сведи результаты book_readability в ОДНУ таблицу (title, "
        "author, cefr_heuristic, flesch_reading_ease, downloads), "
        "отсортируй от простых к сложным (flesch_reading_ease по "
        "убыванию) и порекомендуй 5-7 книг под целевой уровень — по "
        "одной строке обоснования (уровень + длина/популярность). Если "
        "в целевом банде книг мало — честно скажи это и покажи "
        "ближайшие по уровню.\n"
        # B118 (smoke 2.7.8 S3) — рендер бимодально терял join: таблица
        # из top_books, а CEFR-колонка «Не указано в данных», хотя 10
        # book_readability отработали. Join обязателен и проговорён
        # явно, чтобы у модели не было «прозаичного» пути отступления.
        "JOIN ОБЯЗАТЕЛЕН: значения cefr_heuristic и flesch_reading_ease "
        "ЕСТЬ в tool_results (отдельный результат book_readability на "
        "каждую книгу пула; матчи строку по pg_id или title). ЗАПРЕЩЕНО "
        "писать «Не указано в данных» / «—» в колонках CEFR/Flesch для "
        "книги, чей book_readability вернул ok=true — возьми значения "
        "из его data.\n"
        "ЧЕСТНОСТЬ метода (упомяни обязательно): уровень — эвристика "
        "Flesch reading ease по сэмплу текста, НЕ экзаменационная "
        "CEFR-калибровка; кандидаты — топ-10 самых скачиваемых книг "
        "корпуса, а не все 47К. НЕ рекомендуй книги, которых нет в "
        "результатах тулов.",
    ]
    return QueryPlan(
        intent="learning_books", entities=e,
        steps=steps,
        expected_cost="heavy",
        explain=(f"learning_books: top_books_by_downloads(top="
                 f"{_LEARNING_BOOKS_POOL}, lang={lang}) → book_readability "
                 f"× {_LEARNING_BOOKS_POOL} (pg_id@rank) → CEFR-фильтр на "
                 f"рендере ({cefr or e.level or 'min-порог'})"),
        render_notes=notes,
    )


def _plan_lexical_wealth(e: Entities) -> QueryPlan:
    """Phase 3 W-8 (Stan 2026-05-22) — switched the underlying tool from
    `top_authors_by(metric=tokens)` to `lexical_richness_authors`.

    Why: the old tool ranked by raw token VOLUME — that's «who has the
    most text in the corpus», NOT «who has the richest vocabulary».
    Wells/Various/CIA/Library of Congress all topped the charts simply
    because they had a lot of indexed text. The new tool ranks by
    Guiraud R = types / √tokens — length-normalised richness — and
    secondary metrics hapax_ratio / Yule-K / TTR are exposed alongside.

    W-4 is applied inside the wrapper (CIA / Warren Commission etc.
    dropped) so the top of the list is real authors, not aggregates.
    """
    return QueryPlan(
        intent="lexical_wealth", entities=e,
        steps=[PlanStep(tool="lexical_richness_authors",
                        args={"top": e.top_n or 20})],
        expected_cost="heavy",
        explain=("lexical_richness_authors(Guiraud R) — нормированное "
                 "богатство словаря, не объём текста"),
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
        # E36 (2026-05-22) — was leaking `WC_LLM_PLANNER=on` env var name
        # and «спроси админа» to the end user (Stan caught it 2026-05-22).
        # Also had hardcoded dev-test fixture words («tuppence, stitching,
        # embroidery, strychnine, vavasour») that read like prod data leak.
        # Cleaned to neutral guidance without internal-config references
        # or specific words that suggest a particular query.
        return QueryPlan(
            intent="translate_word_list", entities=e, steps=[],
            needs_clarify=True,
            clarify_question=(
                "Не получилось автоматически вытащить слова из "
                "предыдущего ответа — формат списка не распознался.\n\n"
                "Скопируй нужные 5-10 слов и пришли их одним "
                "сообщением, например: «переведи X, Y, Z» — я "
                "подготовлю перевод, IPA и определение для каждого."
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
