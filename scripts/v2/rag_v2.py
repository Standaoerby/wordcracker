"""v2 agentic entry — replaces rag_query.ask{,_stream} when engine=v2.

Pipeline:
  1. classify(intent) — rules first, LLM fallback off by default
  2. extract(entities) — author/book/word/year/country/level/etc
  3. build(plan)       — deterministic tool chain
  4. router.execute*   — runs the plan
  5. renderer          — LLM converts ToolResults into a final answer

The LLM is only invoked at step 5 (renderer) and as a fallback at step 1
when WC_PLANNER_LLM_FALLBACK=on. Tool selection is *not* delegated to the
LLM — that's what removes the Q11-style "narrates plan, never calls tool"
failure mode.

Exposes ask() and ask_stream() with the same shapes v1 emits so chat_server
can route /api/chat/stream?engine=v2 to this module."""
from __future__ import annotations

import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Iterator

import requests

from scripts.v2 import critic as critic_mod
from scripts.v2 import numeric_audit as audit_mod
from scripts.v2 import observability as obs_mod
from scripts.v2.planner import entities as ent_mod
from scripts.v2.planner import history as history_mod
from scripts.v2.planner import intent as int_mod
from scripts.v2.planner import plan as plan_mod
from scripts.v2.planner import router as router_mod
from scripts.v2._types import ToolResult

# Register v2 tools (decorator side effects).
from scripts.v2 import tools as _tools  # noqa: F401

log = logging.getLogger("wordcracker.v2.rag")

DEFAULT_MODEL = os.environ.get("WC_LLM_MODEL", "qwen3:14b")
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Словоёб")

# Render-only prompt — LLM does not see tools.
RENDER_PROMPT = """Тебя зовут {name}. Ты — литературный аналитик корпуса Project Gutenberg.

Тебе пришёл готовый результат запроса от детерминированного планировщика и tool router. Твоя задача — только превратить эти данные в финальный ответ.

⚠️ КРИТИЧЕСКИЕ ПРАВИЛА:
1. **STRICT FACTS-ONLY.** Каждое слово, цифра, цитата, имя в твоём ответе ДОЛЖНО быть в payload.tool_results. Не выводи фактов из «общих знаний». Если факт не в данных — НЕ упоминай его.
2. **Signature words / top words / contexts — ТОЛЬКО из tool data.** Если tool вернул список из 20 слов, не добавляй ни одного слова со стороны, даже если «они тоже типичные для этого автора». Пользователь видел эти слова от critic'а 5 раз — это галлюцинация.
3. **Не выдумывай книги.** Если книги нет в tool data, не упоминай. Pulled название из контекста = галлюцинация.
4. **Не предлагай вызвать ещё tools.** Tool calling завершён.
5. **Язык ответа = язык вопроса пользователя.**
6. **Markdown-таблицы для табличных данных.**
7. **В конце предложи 1-2 next-step вопроса**.
8. Если coverage низкое или есть warning'и — упомяни это.
9. **Если в payload есть поле `render_instructions` — это ПРИОРИТЕТНЫЕ правила, всегда им следуй.**
10. **Если в data есть `_bio_source: "wordcracker hardcoded"` — bio years подтверждены, не сомневайся в них.**
11. **Числа и счёты — буква-в-букву из tool data.** Если ответ говорит «top 10» — в списке ровно 10 элементов из data, не 8 и не 12. Если data показывает 47 — пиши «47», не «около 50» и не «больше 40». Округление в перифразе допустимо только если оно явное («примерно 50, точнее 47»). Никаких чисел из «знания мира» — если число не в data, его нет в ответе. Это включает количество книг, частоты, проценты, годы рождения/смерти, рейтинги, дистанции.
12. **Источник данных — раскрой, если использованы загруженные пользователем книги.** Если в payload есть `user_uploads_used: true`, добавь короткое примечание в конце ответа: «*В ответе использованы загруженные вами книги (U<N>) — это не часть канонического корпуса SPGC*». Список конкретных U-id есть в `user_upload_ids`. На работу tool это не влияет — это исключительно прозрачность.
13. **Copyright disclosure для загруженных copyright-книг.** Если в payload `copyright_book_via_upload: true` — добавь ПОВЕРХ обычного user-upload note ещё один блок: «*© Книга «<copyright_book_title>» находится под защитой copyright. Загружена локально для исследовательских целей (fair use). Запрещены: повторное распространение, копирование больших фрагментов, передача третьим лицам.*» Этот блок ВАЖНЕЕ обычного user-upload note — поставь его первым, обычный — после или объедини в один параграф если контекст позволяет.
14. **Count honesty — единственная истина это `top_returned`.** Если в `data` есть `top_requested` и `top_returned`:
    - Всегда используй именно **`top_returned`** в фразах «список из N слов» / «top N» / «вот N» / «представлен список из N». Это фактический размер списка после всех фильтров (PROPN / surname / OOV / corpus-diff).
    - **Запрещено** писать `top_requested` («50» когда вернулось 19) — это вводит в заблуждение.
    - **Тем более запрещено** придумывать третье число, которого нет ни в data, ни в запросе пользователя (Stan-class hallucination 2026-05-19: user request=100, data top_returned=19, renderer написал «50» — ни одно из них).
    - **АБСОЛЮТНО ЗАПРЕЩЕНО противоречие «таблица из N строк ↔ возвращено 0 / X слов» где X≠N.** Stan 2026-05-20: renderer показал таблицу из 30 прилагательных и при этом написал в footer «было возвращено 0 слов, хотя запрашивалось 30». Это абсурдный self-contradiction. Если ты построил таблицу — count = len(table rows). НИКАКИХ «возвращено 0» в footer когда таблица не пустая. Если таблица пустая — пиши «список пустой / не найдено» и не строй таблицу вовсе.
    - Если хочешь упомянуть оба числа для прозрачности — формулируй явно: «запросил 100, после фильтра имён собственных / OOV-токенов осталось 19». Это окей и часто полезно.
    - Numeric audit и critic в любом случае поймают расхождение — но это пост-фактум, и пользователь уже прочитал кривой ответ. Лучше написать правду сразу.

15. **Метрики — направление и интерпретация ВСЕГДА из tool data, не из общих знаний.** Если в data есть поле `metric_explanations` — используй его описание direction/scale/interpret буквально, не выдумывай. Базовые правила направления (Stan Round 11 B2 hard-block — renderer писал обратное про Burrows Delta):
    - **Burrows Delta** — это РАССТОЯНИЕ стилометрическое. **LOWER value = MORE similar style** (ближе по стилю). НИКОГДА не пиши «чем выше delta, тем сильнее влияние» — это неверно. Stevenson 0.4385 ближе к Doyle чем Twain 0.6021.
    - **Flesch Reading Ease** — выше = легче читать (90-100 школьник, 60-70 средний, 30-50 трудный).
    - **TTR / lexical diversity** — выше = богаче словарь. Sample-size-sensitive — упомяни при сравнении книг разной длины.
    - **PMI / NPMI / Dice (collocates)** — выше = сильнее связь между словами. NPMI нормирован в [-1, 1].
    - **Affinity** — выше = более характерно для автора относительно корпуса (формула в spgc_author_affinity.py).
    - **Jaccard top-200** — выше = больше пересечение топ-200 фирменных слов авторов (similarity, не distance).
    Если direction не очевиден из имени метрики и нет `metric_explanations` — НЕ интерпретируй, просто покажи число.

16. **Book titles > PG ids в user-facing тексте.** Когда tool возвращает книгу с обоими `pg_id` и `title` — ВСЕГДА пиши title в основном тексте ответа («Pride and Prejudice»). PG/U id — это технический ключ для следующих tool calls, не для глаз пользователя. Stan Round 11+ B100: «отдавать не айдишники, а имена книг».
    - Допустимо: «Pride and Prejudice (PG1342)» в скобках при первом упоминании — для прозрачности источника.
    - Допустимо: bare pg_id в clickable-cell metadata (frontend сам решит как показать).
    - Запрещено: «Рекомендую PG2554, PG345, PG1342» как основной текст списка — это unreadable.
    - Если title в data отсутствует (rare lexical_search edge case) — формулируй «книга PG{{id}} (название не извлеклось из метаданных)» и предложи user'у sequence find_book → target tool.

17. **Слова — расширенный ответ (translation + примеры + этимология).** Когда план запустил `enrich_word` ИЛИ `word_contexts` для конкретного слова, и в payload есть данные по нескольким facet'ам (translation_ru, definition_en, etymology, contexts/samples) — surface их ВМЕСТЕ, даже если intent был узкий. Stan B101: «отдавать перевод слова, примеры в контексте и этимологию».
    - Формат: краткое сводное описание сверху (перевод + IPA + POS + определение) → блок «Примеры из корпуса» с 2-3 snippets (если word_contexts отработал) → строка «Этимология» (если etymology в data).
    - НЕ повторяй полный enrich-payload как dump — только полезные поля.
    - Если какой-то facet отсутствует (например etymology=None) — мягко упомяни «этимология не извлеклась» один раз, не висни.

18. **Tabular data — ОБЯЗАТЕЛЬНО markdown table.** Если в `data` есть массив с >2 объектами одинаковой shape (top/top_words/matches/samples/top_unique_a/affinity rows) — ОБЯЗАН вывести markdown pipe-table. Stan Round 13 Q13: запрос «сколько книг у Marlowe» в R12 показал 12 произведений таблицей, в R13 — «количество не указано напрямую» прозой. Это **потеря данных** из-за renderer non-determinism.
    - Правило: array of similar dicts → table.
    - Header — meaningful column names в языке user'а («Название», «Автор», «Год», «Affinity»). Pg_id в скобках при title.
    - Не разворачивай таблицу в прозу. Не «пересказывай» что в таблице — таблица сама.
    - Если array пустой (n=0) — НЕ строй пустую таблицу, скажи «список пустой».
    - Если array из 1-2 объектов — можно текстом или mini-table; выбирай по читабельности.

19. **Скрывать колонки без данных (W-3, 2026-05-23).** Колонка таблицы либо заполнена реальным значением, либо её НЕТ в выводе. Если у тебя в данных все строки имеют отсутствующее / None / пустое значение для какого-то поля — НЕ добавляй колонку с этим полем. Не пиши столбец, где у всех строк «—». Пейлоад уже отнормирован: если ключ X отсутствует у всех строк — он удалён до тебя; ты НЕ должен его восстанавливать «логически» (типа «может быть, есть IPA — добавлю пустую колонку IPA»). Колонка показывается тогда и только тогда, когда хотя бы у одной строки есть конкретное значение. Per-row «—» в смешанной колонке допустим (честное «нет данных по ячейке»), а целая колонка «—» — запрещена.

20. **Антивыдумка фактов (W-7, 2026-05-23, ужесточено 2026-05-24).** Это правило сильнее всех остальных. ЛЮБОЕ из перечисленного НИЖЕ ДОЛЖНО присутствовать буквально в `tool_results[*].data` (или его вложенных полях) — иначе ты пишешь «не указано в данных» и НЕ выдумываешь:
    - **Числа и счёты** — количество книг/слов/упоминаний/частот/процентов/рейтингов/дистанций/индексов.
    - **Годы** — рождения, смерти, публикации, любые даты. Stan prod-bug 2026-05-22: рендер написал «год смерти Marlowe 2008» — чистая галлюцинация, в data нет death_year. Правильно: «год смерти не указан в данных».
    - **ID** — PG-id (PG1342), U-id (U7), ISBN, любые идентификаторы. Не «PG12345 — Crime and Punishment», если PG12345 не в matches/data.
    - **Имена** — авторов, книг, персонажей, мест. Не упоминай «The Bookbinder's Apprentice», если этого названия нет в matches/data. Не упоминай «Watson», если он не появился в snippets/contexts.
    - **Координаты, ссылки, URL** — только из data.

    **Anti-knowledge clause (КРИТИЧНО):** Это правило **перекрывает** твои общие знания об авторах и произведениях. Даже если ты ТОЧНО знаешь из обучения, что Christopher Marlowe умер в 1593, а в data только `birth_year: 1564` — пиши «год смерти не указан в данных». Даже если ты знаешь, что у Толстого ≈90 томов академического собрания, а в data `books_in_corpus: 47` — пиши **47** и только 47. Знание-из-обучения для тебя сейчас как факт о соседе: «может, правда, может, нет, проверить нечем». Только tool_results — истина.

    **Протокол генерации факта.** Прежде чем написать любое число/год/id/имя:
    1. Найди его конкретно в `tool_results[*].data` (включая вложенные структуры).
    2. Если нашёл — пиши **буквально** (округление только явное: «47, около 50»).
    3. Если НЕ нашёл — пиши «не указано в данных» / «нет в выдаче» / «коrпус не показывает». НЕ подставляй число из «общих знаний», НЕ оставляй пустое поле, НЕ выдумывай похожее.
    4. Если в сомнении «было ли это в data» — считай «не было», действуй по п.3.

    Конкретные исторические Stan-class фабрикации (которых быть не должно):
    - «год смерти Marlowe 2008» — 2008 не из data, верный ответ «не указано».
    - «1000 книг у Marlowe» — в data `books_in_corpus: 200`, верный ответ «200 книг».
    - «PG12345 = Crime and Punishment» — PG12345 не из matches.
    - «прочитанные слова: factory, machine, smoke, …» когда tool вернул только factory.

    Critic и numeric_audit ловят часть этих случаев пост-фактум, но пользователь уже видит ложь. Лучше написать «не указано в данных» сразу.

Tool trace тебе передан как JSON. Каждая запись — {{tool, query, data, warnings, coverage}}. Возьми оттуда factual content. **Ничего вне этих данных.** Помни: «не указано в данных» — это **полноценный честный ответ**, а не пробел, который надо заполнить из своих знаний."""


# ---------- Sprint 14: LLM-fallback entity merge ----------


# Intents whose plan-builder needs at least one of these entities. When the
# rule fires the right intent but regex missed the entity, escalate to
# `classify_and_extract` for entity-only LLM help.
_INTENTS_REQUIRING_ENTITIES = {
    "author_vocab":      ("author_regex",),
    "author_top_words":  ("author_regex",),
    "author_compare":    ("author_regex",),
    "author_closest":    ("author_regex",),
    "author_influences": ("author_regex",),
    "author_metadata":   ("author_regex",),
    "author_lookup":     ("author_regex",),  # Sprint 16 Phase E
    "country_vocab":     ("author_regex",),
    "vocab_passport":    ("author_regex",),
    "lexical_wealth":    (),  # global query, no entity required
    "book_vocab":        ("book_id", "book_title"),
    "book_readability":  ("book_id", "book_title"),
    "book_archaic":      ("book_id", "book_title"),
    "book_emotion":      ("book_id", "book_title"),
    "book_compare":      ("book_id", "book_title"),
    "book_lookup":       ("book_title", "book_id"),
    "book_pub_year":     ("book_id", "book_title"),  # Sprint 16 Phase G
    "book_similar":      ("book_id", "book_title"),  # Sprint 17
    "word_contexts":     ("word",),
    "word_collocates":   ("word",),
    "word_timeline":     ("word",),
    "word_pos":          ("word",),
    "word_etymology":    ("word", "etymology_family"),
}


def _needs_entity_help(intent_label: str, entities) -> bool:
    """True iff this intent requires entities and regex extractor missed
    them all. Used to gate the entity-only LLM fallback."""
    required = _INTENTS_REQUIRING_ENTITIES.get(intent_label)
    if not required:
        return False
    for field in required:
        if getattr(entities, field, None):
            return False
    return True


# Canonical surname → AUTHOR_ALIASES regex. Built lazily once.
_SURNAME_LOOKUP: dict[str, str] | None = None


def _surname_to_regex(name: str) -> str | None:
    """Convert an LLM-suggested author surname («Doyle», «Tolstoy») to the
    `^Surname,` regex shape AUTHOR_ALIASES uses. Returns None when we
    don't recognize the surname."""
    if not name:
        return None
    global _SURNAME_LOOKUP
    if _SURNAME_LOOKUP is None:
        from scripts.v2.planner.entities import AUTHOR_ALIASES
        # AUTHOR_ALIASES values look like "^Doyle," — extract the surname
        # part as a lookup key.
        _SURNAME_LOOKUP = {}
        for regex in set(AUTHOR_ALIASES.values()):
            # regex shape: ^Surname,  (or ^Bront for prefix matches)
            import re
            m = re.match(r"\^([A-Za-zЀ-ӿ'-]+)", regex)
            if m:
                _SURNAME_LOOKUP[m.group(1).lower()] = regex
    return _SURNAME_LOOKUP.get(name.strip().lower())


# Canonical title lookups for LLM-suggested book titles. Reuses
# KNOWN_BOOKS so we get the same PG ids the rule extractor uses.
def _title_to_book(title: str) -> tuple[str | None, str | None]:
    """Given an LLM-suggested title, look up (pg_id, canonical_title) via
    KNOWN_BOOKS. Falls back to (None, raw_title) so the find_book tool
    can resolve it dynamically."""
    if not title:
        return None, None
    from scripts.v2.planner.entities import KNOWN_BOOKS
    key = title.lower().strip().strip("\"'«»“”")
    if key in KNOWN_BOOKS:
        pg, canonical = KNOWN_BOOKS[key]
        return (pg if pg else None), canonical
    # Try leading-«the» fuzzy match (same trick as in plan.py).
    if not key.startswith("the "):
        alt = "the " + key
        if alt in KNOWN_BOOKS:
            pg, canonical = KNOWN_BOOKS[alt]
            return (pg if pg else None), canonical
    return None, title.strip()


def _merge_llm_entities(entities, llm_full: dict) -> None:
    """Mutate `entities` in place: fill missing fields from LLM-suggested
    extraction. Regex wins where it found something — LLM only fills
    gaps. Keeps the dataclass shape and types intact so downstream
    plan-builder doesn't need any awareness of where the entity came
    from."""
    if not isinstance(llm_full, dict):
        return
    if entities.author_regex is None:
        author = llm_full.get("author")
        if author:
            regex = _surname_to_regex(author)
            if regex:
                entities.author_regex = regex
                entities.author_label = author
    if entities.book_title is None and entities.book_id is None:
        title = llm_full.get("book_title")
        if title:
            pg, canonical = _title_to_book(title)
            if pg:
                entities.book_id = pg
            if canonical:
                entities.book_title = canonical
    if entities.word is None:
        word = llm_full.get("word")
        if word and isinstance(word, str) and 2 <= len(word) <= 30:
            entities.word = word.strip().lower()
    if entities.year_from is None:
        yf = llm_full.get("year_from")
        if isinstance(yf, int) and 1500 <= yf <= 2100:
            entities.year_from = yf
    if entities.year_to is None:
        yt = llm_full.get("year_to")
        if isinstance(yt, int) and 1500 <= yt <= 2100:
            entities.year_to = yt
    if entities.country is None:
        country = llm_full.get("country")
        if country and isinstance(country, str) and len(country) == 2:
            entities.country = country.upper()


# ---------- Sprint 16 Phase A3: unknown author candidate telemetry ----------


# Common-word stoplist: capitalized tokens at sentence start, English
# nouns / titles that shouldn't trigger «unknown author» false alarms.
_AUTHOR_CANDIDATE_STOPLIST = frozenset({
    # Sentence starters (RU) — title case typo / Russian first-letter
    "Какие", "Какой", "Какая", "Когда", "Почему", "Где", "Кто", "Что",
    "Найди", "Покажи", "Дай", "Сравни", "Топ", "Сколько", "Слово",
    "Слова", "Топ", "Что", "Список", "Пример", "Примеры",
    # Sentence starters (EN)
    "What", "Who", "When", "Where", "Why", "How", "Show", "Find", "Give",
    "List", "Top", "Compare", "Words", "Word", "The",
    # Common literary terms that get capitalized
    "Romance", "Gothic", "Victorian", "Edwardian", "Russian", "English",
    "American", "British", "French", "German", "Italian",
    # Days / months / years caught accidentally
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    "Sunday",
    # Common short proper-noun-ish tokens
    "AI", "PG", "LLM", "ChromaDB", "OK", "FAQ", "Anki",
})


def _detect_unknown_author_candidates(text: str, entities) -> list[str]:
    """Return capitalized 4+ char tokens in `text` that look like author
    surnames but weren't matched by entities.extract().

    Heuristics:
    - Token starts with uppercase letter
    - Length 4-25 chars
    - All letters (no digits/punctuation in middle)
    - Not in stoplist (common starters / nouns)
    - Not already matched as author by regex extractor (would shadow)
    - At most 3 candidates returned per query — beyond that = noise

    Goal: Stan sees «query: 'что у вас про Уолпол?' → no_author_extracted,
    candidate: Уолпол» in admin /failed and knows what to add to aliases
    next time. Foundation for «promote to alias» button (Phase H3, deferred).
    """
    if not text or entities.author_regex:
        return []
    import re
    # Find capitalized tokens 4-25 chars, alphabetic + apostrophe + hyphen
    tokens = re.findall(
        r"\b([A-ZА-ЯЁ][a-zа-яё.''-]{3,24})\b",
        text,
    )
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t in _AUTHOR_CANDIDATE_STOPLIST:
            continue
        if t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
        if len(out) >= 3:
            break
    return out


# ---------- LLM call (renderer only) ----------


def _conversation_summary(history: list[dict] | None, plan: plan_mod.QueryPlan) -> dict:
    """Compact summary of prior turns for the renderer — points to the
    last resolved author/book/word so the model can say «как мы уже
    видели» instead of re-asking. Mirrors what history.merge_with_history
    already extracted at planner time."""
    e = plan.entities
    return {
        "current_author": e.author_label or e.author_regex,
        "current_book_id": e.book_id,
        "current_book_title": e.book_title,
        "current_word": e.word,
        "current_country": e.country,
        "current_year_range": [e.year_from, e.year_to] if (e.year_from or e.year_to) else None,
        "turns_in_history": len(history or []),
    }


def _harvest_view_filter_values(view) -> dict:
    """E17 (2026-05-22) — extract filter-value numbers from a RenderableView
    that the template_executor renders as part of an «Применённые
    фильтры:» block or provenance. These are not in tool data but ARE
    in the user-visible answer, so numeric_audit must treat them as
    legitimate sources.

    Returns a flat dict {key: value} mixing empty_state.filters_applied
    + provenance.requested + provenance.filtered. Keys are not used by
    the audit (it walks values), but the dict shape lets numeric_audit's
    _walk_numbers recurse naturally.
    """
    if view is None:
        return {}
    out: dict = {}
    try:
        es = getattr(view, "empty_state", None)
        if es is not None:
            fa = getattr(es, "filters_applied", None)
            if isinstance(fa, dict):
                out["empty_filters_applied"] = fa
        prov = getattr(view, "provenance", None)
        if prov is not None:
            req = getattr(prov, "requested", None)
            if isinstance(req, dict):
                out["provenance_requested"] = req
            filt = getattr(prov, "filtered", None)
            if isinstance(filt, dict):
                out["provenance_filtered"] = filt
            ret = getattr(prov, "returned", None)
            if isinstance(ret, dict):
                out["provenance_returned"] = ret
    except Exception:
        pass
    return out


def _collect_render_instructions(results: list[ToolResult]) -> list[str]:
    """Pull `_render_note` strings out of every tool's data payload and
    promote them into a top-level field the LLM sees first. The Qwen3
    renderer was missing notes buried inside `data` — surfacing them
    explicitly makes Q1 (index conflation), Q12 (publication-vs-life
    years), and cosine=0 cases land on the user with the right caveat."""
    notes: list[str] = []
    for r in results:
        if not isinstance(r.data, dict):
            continue
        note = r.data.get("_render_note")
        if isinstance(note, str) and note.strip():
            notes.append(f"[{r.tool}] {note.strip()}")
    return notes


# W-3 (2026-05-23) — empty-column normalization for the LLM render payload.
#
# Cross-cutting prod bug: tools emit list-of-dict rows where some columns
# are None across ALL rows (e.g. `npmi` when metric=count; `translation_ru`
# when learning_words ran without enrichment; `cosine_similarity` when v1
# didn't compute). The LLM, following RENDER_PROMPT rule 18 («array of
# similar dicts → table»), faithfully renders every key as a column —
# including the all-None ones, which show up as a column of «—» cells.
#
# RENDER_PROMPT rule 19 tells the LLM to hide such columns; this helper
# enforces it deterministically by stripping all-empty keys from the rows
# BEFORE sending. The LLM never sees the key, so it cannot render it.
#
# Per-row «—» (some rows have the value, some don't) is still allowed and
# rendered honestly. Only the «entire column is empty» case is stripped.
#
# Identity fields (`pg_id`, `id`, `title`, `author`, `word`, `lemma`,
# `name`, `slug`, `regex`) are protected — they're always meaningful even
# when missing, and stripping them would break the LLM's understanding of
# what each row represents.

_IDENTITY_KEYS_NEVER_STRIP = frozenset({
    "pg_id", "id", "uid", "u_id",
    "title", "book_title", "name", "author", "author_canonical",
    "word", "lemma", "token", "ngram",
    "rank", "position", "emotion", "kind", "label", "key",
    "regex", "slug", "scope_label",
})

# Some list-keys carry rows we definitely want to normalize. Others
# (matches sets, system fields) we leave alone. The walker is conservative:
# it only normalizes lists where every element is a dict — opaque lists
# pass through untouched.


def _is_value_empty(v) -> bool:
    """True iff `v` should be considered «no data» for column-hiding."""
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return True
        if s.lower() == "none":
            return True
        return False
    if isinstance(v, (list, dict)):
        return not v
    # numbers (incl. 0), bools — meaningful, not empty
    return False


def _strip_empty_keys_from_rows(rows: list[dict]) -> list[dict]:
    """Drop keys that are empty across ALL rows. Returns NEW row dicts
    (does not mutate originals). Identity keys are never stripped."""
    if not rows:
        return rows
    # Only operate on lists of homogeneous dicts. Otherwise pass through.
    if not all(isinstance(r, dict) for r in rows):
        return rows
    # Build the union of keys actually appearing across rows.
    all_keys: set[str] = set()
    for r in rows:
        all_keys.update(r.keys())
    drop: set[str] = set()
    for k in all_keys:
        if k in _IDENTITY_KEYS_NEVER_STRIP:
            continue
        if k.startswith("_"):
            # Internal markers (e.g. _word_for_filter) — pass through;
            # they're stripped at the json.dumps layer if needed.
            continue
        # Hide the key iff every row has an «empty» value for it.
        if all(_is_value_empty(r.get(k)) for r in rows):
            drop.add(k)
    if not drop:
        return rows
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def _normalize_data_for_render(data):
    """Walk a tool's `data` dict, strip all-empty columns from any
    list-of-dict it carries. Returns a NEW dict (deep-ish copy — lists
    are rebuilt, leaves shared). Non-dict input passes through.

    The walk is shallow on purpose: tools put their rows directly under
    top-level keys (`top`, `samples`, `matches`, `top_collocates`,
    `top_unique_a/b`, `timeline`, `series`, `emotions`, ...). Deep
    recursion would over-fit and could touch fields the LLM relies on
    (e.g. nested `author1.top_unique`).

    Exception: compare_authors nests `top_unique` inside `author1` /
    `author2` dicts. We descend one level into dict-valued keys and
    apply the row-strip there too."""
    if not isinstance(data, dict):
        return data
    out: dict = {}
    for k, v in data.items():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            out[k] = _strip_empty_keys_from_rows(v)
        elif isinstance(v, dict):
            # Descend one level — covers compare_authors-style nesting
            # where author1/author2 wraps the actual row list.
            inner: dict = {}
            for ik, iv in v.items():
                if (isinstance(iv, list) and iv
                        and all(isinstance(x, dict) for x in iv)):
                    inner[ik] = _strip_empty_keys_from_rows(iv)
                else:
                    inner[ik] = iv
            out[k] = inner
        else:
            out[k] = v
    return out


def _normalize_payload_tool_results(summary_payload: dict) -> dict:
    """Apply `_normalize_data_for_render` to every entry in
    `summary_payload['tool_results']`. Returns a NEW summary_payload;
    the original (and the underlying ToolResult.data) is not mutated.

    Called from `_llm_render` right before the JSON serialise step.
    Tested via test_render_payload_normalize.py."""
    if not isinstance(summary_payload, dict):
        return summary_payload
    tr = summary_payload.get("tool_results")
    if not isinstance(tr, list):
        return summary_payload
    new_tr = []
    for entry in tr:
        if not isinstance(entry, dict):
            new_tr.append(entry)
            continue
        e2 = dict(entry)
        e2["data"] = _normalize_data_for_render(entry.get("data"))
        new_tr.append(e2)
    out = dict(summary_payload)
    out["tool_results"] = new_tr
    return out


# Sprint 18 — retrieval source logging. Which tools surface ranked
# chunks/books that go into the renderer prompt → log them so we can
# diagnose «модель плохо ответила» vs «ей дали мусор» separately.
_RETRIEVAL_TOOLS = frozenset({
    "hybrid_search", "find_book_by_topic", "word_contexts",
    "word_contexts_global", "semantic_search", "lexical_search",
})


# Sprint 19 — detect user-uploaded book references (U-prefix ids) in
# tool results. When present, the renderer adds a disclosure footer
# («ответ частично из загруженных вами книг») so the user knows the
# answer isn't drawn purely from the canonical SPGC corpus.
import re as _re_uid
_U_ID_PATTERN = _re_uid.compile(r"\bU\d+\b")


def _detect_user_uploads(results: list[ToolResult]) -> tuple[bool, int, list[str]]:
    """Walk every ToolResult.data recursively; collect distinct U-ids.

    Returns (has_uploads, count, sample_ids[:5]) — the sample is for
    the render hint so the LLM can reference specific ids if helpful."""
    seen: set[str] = set()
    for r in results:
        if not r.ok or r.data is None:
            continue
        try:
            blob = json.dumps(r.data, ensure_ascii=False, default=str)
        except Exception:
            continue
        for m in _U_ID_PATTERN.finditer(blob):
            seen.add(m.group(0))
            if len(seen) >= 50:  # cap to bound work
                break
    return bool(seen), len(seen), sorted(seen)[:5]


def _detect_copyright_via_uploads(entities) -> tuple[bool, str | None]:
    """Sprint 19+ — when the entity extractor resolved a KNOWN_BOOKS
    empty-PG title (copyright sentinel like Harry Potter / LOTR / 1984)
    to a U-id (via find_user_upload_for_title), the chat must add a
    fair-use / no-redistribution disclaimer. Detect that combo here.

    Returns (is_copyright_upload, canonical_title) — the title is
    used in the renderer footer so the LLM names the specific work."""
    if not entities.book_id or not entities.book_id.startswith("U"):
        return False, None
    if not entities.book_title:
        return False, None
    try:
        from scripts.v2.planner.entities import KNOWN_BOOKS
    except ImportError:
        return False, None
    # Look up the title in KNOWN_BOOKS — if it's there with an empty
    # PG sentinel, this is a known copyright work the user has
    # uploaded locally.
    title_lc = entities.book_title.lower().replace("’", "'").replace("‘", "'")
    for key, (pg, canonical) in KNOWN_BOOKS.items():
        if pg:
            continue  # has PG id → not a copyright sentinel
        if title_lc in key or key in title_lc or title_lc == canonical.lower():
            return True, canonical
    return False, None


def _extract_retrieval_log(results: list[ToolResult],
                            limit: int = 12) -> list[dict] | None:
    """Build a compact retrieval-source list for obs_mod.log_request.

    Walks tool results, keeps only those from RAG-ish tools, captures
    {tool, pg_id, score, snippet_preview, title?, author?} for each
    match. Caps at `limit` rows total across all tools so logs stay
    bounded. Returns None when there's nothing to log (no retrieval
    tool ran)."""
    rows: list[dict] = []
    for r in results:
        if r.tool not in _RETRIEVAL_TOOLS or not r.ok:
            continue
        data = r.data or {}
        matches = data.get("matches") or data.get("samples") or []
        if not isinstance(matches, list):
            continue
        for m in matches:
            if not isinstance(m, dict):
                continue
            row = {"tool": r.tool}
            for key in ("pg_id", "id", "title", "author"):
                v = m.get(key)
                if v is not None:
                    row[key] = v
            # Score: prefer rerank if present, else rrf, else generic
            for key in ("rerank_score", "rrf_score", "score", "distance"):
                v = m.get(key)
                if isinstance(v, (int, float)):
                    row["score"] = round(float(v), 4)
                    row["score_kind"] = key
                    break
            snippet = m.get("snippet") or m.get("text") or m.get("body")
            if isinstance(snippet, str) and snippet.strip():
                row["snippet_preview"] = snippet.strip()[:120]
            rows.append(row)
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    return rows or None


# Sprint 22+ alpha6 — table-heavy intents get lower renderer temp.
# Round 13 Q13 (Marlowe books) and Q20 (P&P word counts) showed
# renderer non-determinism on tabular data: same tool result, two
# different renderings (one with table + counts, one with «не указано»
# prose). Temp=0.1 makes the renderer more deterministic on these
# intents. Prose-heavy intents (author_compare interpretation,
# introduction, clarify text) keep 0.3 for natural variation.
_LOW_TEMP_INTENTS: frozenset[str] = frozenset({
    "top_authors_books",
    "author_vocab",
    "author_top_words",
    "author_metadata",
    "author_lookup",
    "author_closest",
    "author_influences",
    "author_compare",
    "book_vocab",
    "book_lookup",
    "book_compare",
    "book_recommendation",
    "book_similar",
    "book_pub_year",
    "book_archaic",
    "book_emotion",
    "book_readability",
    "book_readability_compare",
    "topic_book_search",
    "word_collocates",
    "word_etymology",
    "word_timeline",
    "word_pos",
    "word_dialogue",
    "word_movement",
    "country_compare",
    "country_vocab",
    "period_vocab",
    "composite_compare",
    "genre_compare",
    "topic_words",
    "lexical_wealth",
    "vocab_passport",
    "learning",
    "corpus_extremum",
    "book_extremum",
    "translate_word_list",
    "export_word_list",
})


# Sprint 22+ alpha5 — render-time token budget (replaces alpha4 magic-
# number truncation). See scripts/v2/token_budget.py for design.
# Stan 2026-05-20 Christie case: 200-item tool result blew num_ctx →
# confabulation. The TokenBudget layer adaptively shrinks payload via
# a 7-step ladder until it fits OR exhausts options (then honest fail).
#
# Public alias `_truncate_for_render` kept for backwards-compat with
# tests; delegates to TokenBudget for actual work. Magic constants
# RENDER_LIST_CAP/RENDER_STR_CAP gone — budget is dynamic per call.

from scripts.v2.token_budget import TokenBudget, ShrinkReport


def _truncate_for_render(obj, _depth: int = 0):
    """Compatibility shim — applies a generic cap (50 lists, 2000
    strings) via TokenBudget helpers without computing actual budget.
    Used by tests that pre-date the budget API.
    """
    from scripts.v2.token_budget import _cap_lists, _cap_strings
    out, _ = _cap_lists(obj, cap=50)
    out, _ = _cap_strings(out, cap=2000)
    return out


def _llm_render(question: str, plan: plan_mod.QueryPlan,
                results: list[ToolResult], *, model: str,
                ollama_host: str,
                history: list[dict] | None = None,
                cancel_event=None) -> tuple[str, dict]:
    """Send one /api/chat call with the render prompt + tool data. No tools.

    Sprint 17: returns (answer_text, meta) where meta carries Ollama's
    prompt_eval_count + eval_count for observability. The token counts
    feed obs_mod.log_request so the admin dashboard can answer «do we
    need more num_ctx» data-driven instead of by guessing."""
    render_instructions = _collect_render_instructions(results)
    # Sprint 20+ — plan-level render notes (e.g. exclude_archaic flag).
    # These are stamped by the plan builder, not by any individual tool,
    # so they aren't reachable via data._render_note.
    for note in getattr(plan, "render_notes", []) or []:
        if isinstance(note, str) and note.strip():
            render_instructions.append(f"[plan] {note.strip()}")
    # Sprint 19 — surface user-upload usage to the renderer.
    has_uploads, upload_count, upload_sample = _detect_user_uploads(results)
    # Sprint 19+ — copyright-via-upload (HP / LOTR / 1984 / etc.
    # uploaded locally). Renderer adds the fair-use disclaimer per
    # RENDER_PROMPT rule 13.
    is_copyright_upload, copyright_title = _detect_copyright_via_uploads(
        plan.entities)
    summary_payload = {
        "intent": plan.intent,
        "explain": plan.explain,
        "conversation_context": _conversation_summary(history, plan),
        # Top-level priority instructions — the renderer was missing notes
        # buried inside per-tool `data`. Surface them explicitly.
        "render_instructions": render_instructions,
        # Source transparency — see RENDER_PROMPT rules 12 + 13.
        "user_uploads_used":      has_uploads,
        "user_upload_count":      upload_count,
        "user_upload_ids":        upload_sample,
        "copyright_book_via_upload": is_copyright_upload,
        "copyright_book_title":      copyright_title,
        "tool_results": [
            {
                "tool": r.tool,
                "query": r.query,
                "data": r.data,
                "warnings": [{"code": w.code, "message": w.message}
                             for w in r.warnings],
                "coverage": {"books_matched": r.coverage.books_matched,
                             "books_total": r.coverage.books_total},
                "ok": r.ok,
                "error": ({"type": r.error.type, "message": r.error.message}
                          if r.error else None),
            }
            for r in results
        ],
    }

    # W-3 (2026-05-23) — strip all-empty columns from tool_results before
    # the LLM sees them. RENDER_PROMPT rule 19 reinforces this on the
    # model side; this pre-pass guarantees it deterministically (the LLM
    # cannot render a column for a key it never received). Applied BEFORE
    # the token budget shrink so size estimates reflect what actually
    # ships. See _normalize_payload_tool_results docstring for scope.
    summary_payload = _normalize_payload_tool_results(summary_payload)

    # Sprint 22+ alpha5 — Token Budget Layer. Adaptive shrink before
    # send to Ollama. Replaces alpha4 hard-capped _truncate_for_render
    # which used magic numbers irrespective of actual ctx pressure.
    # Budget = ctx - headroom (default 1500 for response). When fits,
    # zero work. When over, ladder kicks in. When ladder exhausts,
    # report.fits=False — we log WARN but still attempt (phase-1
    # transitional; phase-4 will hard-fail).
    budget = TokenBudget(model=model)
    summary_payload, shrink_report = budget.shrink_to_fit(summary_payload)
    if shrink_report.actions:
        log.info("renderer shrink applied: %s (initial=%d → final=%d, "
                 "budget=%d, util=%d%%)",
                 shrink_report.actions, shrink_report.initial_tokens,
                 shrink_report.final_tokens, shrink_report.budget,
                 shrink_report.utilization_pct())
    if not shrink_report.fits:
        log.warning("renderer payload STILL over budget after ladder "
                    "exhausted: %s (estimate=%d, budget=%d)",
                    shrink_report.actions, shrink_report.final_tokens,
                    shrink_report.budget)

    messages = [
        {"role": "system", "content": RENDER_PROMPT.format(name=ASSISTANT_NAME)},
        {"role": "user", "content": question},
        {"role": "user",
         "content": "Tool data:\n```json\n"
                    + json.dumps(summary_payload, ensure_ascii=False, default=str)
                    + "\n```"},
    ]
    # Sprint 22+ alpha6 — temperature по intent. R13 показал что
    # на 4 из 20 запросов renderer давал мелкую variance на табличных
    # данных (Q13 Marlowe пропустил таблицу; Q20 пропустил word count).
    # Для table-heavy intents — temp 0.1 (детерминированнее, числа и
    # колонки стабильнее). Для прозы — стандартный 0.3.
    temp = 0.1 if (plan.intent in _LOW_TEMP_INTENTS) else 0.3
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        # Sprint 22+ alpha5: explicit num_ctx in options. Without this,
        # Ollama uses whatever default is baked into the model
        # (typically 8192 for qwen3:14b). With WC_OLLAMA_NUM_CTX set
        # (env or default 16384 in MODEL_CTX_DEFAULTS), we get a
        # bigger window and matching budget.
        "options": {"temperature": temp, "num_ctx": budget.ctx},
        "think": False,
    }
    # S-P2b — stream the render so an orphaned generation (client dropped
    # the SSE mid-answer) can be cancelled. Closing the socket makes
    # Ollama 0.24 abort the in-flight gen and free the single runner slot
    # (~2.7s, confirmed by repro 2026-06-02). cancel_event is set by the
    # SSE pump (chat_server._stream_chat) on client disconnect; default
    # None -> a private never-set Event so non-streaming callers (ask())
    # are unaffected.
    if cancel_event is None:
        cancel_event = threading.Event()
    payload["stream"] = True
    parts: list[str] = []
    last_obj: dict = {}
    resp = requests.post(f"{ollama_host}/api/chat", json=payload,
                         stream=True, timeout=120)
    try:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if cancel_event.is_set():
                resp.close()
                raise RenderCancelled("client disconnected during render")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, TypeError):
                continue
            last_obj = obj
            piece = (obj.get("message") or {}).get("content") or ""
            if piece:
                parts.append(piece)
    finally:
        resp.close()
    body = last_obj or {}
    try:
        from scripts.v2.observability import log_llm_latency
        log_llm_latency("renderer", model, budget.ctx, body)
    except Exception:
        pass
    text = "".join(parts).strip()
    meta = {
        "prompt_tokens": body.get("prompt_eval_count"),
        "eval_tokens":   body.get("eval_count"),
        "total_duration_ns": body.get("total_duration"),
        "load_duration_ns":  body.get("load_duration"),
        # Sprint 22+ alpha5 — token-budget observability per call.
        # Admin dashboard can plot utilization trends + correlate
        # confabulation_risk:high with user-reported bad answers.
        **budget.to_log_dict(shrink_report),
    }
    return text, meta


# ---------- Pipeline envelope (trace + budget) ----------


def _v5_pipeline_envelope(question: str, *, engine: str = "v2"):
    """Create RequestTrace + RequestBudget for the request.

    Lightweight: trace is append-only; budget only carries the contract,
    not enforced here (Phase 5 chokepoint will tighten enforcement).
    Returns None on init failure so callers degrade gracefully.

    Name kept for historical compat; envelope is always created in
    Phase 1+ (previously gated by WC_V5_PIPELINE — flag removed)."""
    try:
        from scripts.v2 import observability as _obs
        from scripts.v2 import budget as _b
        return {
            "trace": _obs.start_trace(question, engine=engine),
            "budget": _b.RequestBudget(),
            "t0": time.perf_counter(),
        }
    except Exception as e:
        log.warning("pipeline envelope init failed: %s", e)
        return None


def _v5_budget_from_envelope(envelope, *, intent_label: str | None = None):
    """v5 Phase 5 — derive a RequestBudget for the router from the v5
    envelope. Returns None when envelope absent → router runs unbounded
    (legacy behaviour preserved when flag off).

    When envelope present, budget defaults to per-intent value
    (`RequestBudget.for_intent(intent)`). Router aborts via
    `budget_exceeded` event if it goes over.

    Closes Q25/Q114/Q105 latency class structurally: no plan can run
    past the request envelope. find_book_by_topic spending 5 minutes
    on a single tool call gets aborted at ~30-60s (per-intent budget)
    and surfaces ERROR_FRIENDLY view with partial results.
    """
    if envelope is None:
        return None
    try:
        from scripts.v2 import budget as _b
        b = _b.RequestBudget.for_intent(intent_label)
        # Phase 5: anchor budget clock to envelope start so remaining_s()
        # automatically subtracts planner / entity-resolution elapsed time.
        # dispatch chokepoint uses min(spec.timeout_s, budget.remaining_s)
        # — no need to mutate wall_clock_s manually.
        b.set_clock(envelope["t0"])
        envelope["budget"] = b
        return b
    except Exception as e:
        log.warning("v5 budget derive failed: %s", e)
        return None


def _v5_envelope_extras(envelope, *, intent_label: str | None = None,
                        render_meta: dict | None = None) -> dict:
    """Build extra log fields from a v5 envelope. Empty dict if no
    envelope — caller spreads via `**extras` into log_request."""
    if envelope is None:
        return {}
    elapsed = time.perf_counter() - envelope["t0"]
    budget = envelope["budget"]
    extras = {
        "v5_trace_id": envelope["trace"].trace_id,
        "v5_pipeline_version": envelope["trace"].pipeline_version,
        "v5_budget_max_s": budget.wall_clock_s,
        "v5_budget_used_s": round(elapsed, 2),
        "v5_budget_exceeded": elapsed > budget.wall_clock_s,
    }
    if intent_label is not None:
        envelope["trace"].set_intent(intent_label)
        extras["v5_intent"] = intent_label
    if render_meta:
        extras["v5_render_view_type"] = render_meta.get("view_type")
        extras["v5_render_prose_used"] = render_meta.get("prose_used", False)
        extras["v5_render_audit_failed"] = render_meta.get("prose_audit_failed", False)
        extras["v5_render_phase_a_ms"] = render_meta.get("phase_a_ms", 0)
        extras["v5_render_phase_b_ms"] = render_meta.get("phase_b_ms", 0)
        if render_meta.get("fallback_reason"):
            extras["v5_render_fallback"] = render_meta["fallback_reason"]
        envelope["trace"].set_render(
            view_type=render_meta.get("view_type"),
            phase_a_ms=render_meta.get("phase_a_ms", 0),
            phase_b_ms=render_meta.get("phase_b_ms", 0),
            phase_b_used=render_meta.get("prose_used", False),
            skeleton_chars=render_meta.get("skeleton_chars", 0),
        )
    return extras


# ---------- Render dispatcher ----------


_CONTENT_LIST_KEYS = (
    "matches", "samples", "top", "rows", "results",
    "candidates", "items", "series", "timeline",
    "top_collocates", "top_unique_a", "top_unique_b",
    "top_authors", "top_books", "top_words",
)

# Diagnostic / metadata fields that should NOT make a result count as
# «has useful data» by themselves. If a tool returned ONLY these keys
# with no real payload, the friendly-error guard should still fire.
_METADATA_KEYS = frozenset({
    "query", "word", "author_regex", "scope", "scope_label",
    "k", "k_rrf", "top_requested", "lexical_n", "semantic_n",
    "min_corpus_count", "total_occurrences", "reranked_by",
    "lang", "language", "intent",
})


def _has_useful_data(r: ToolResult) -> bool:
    """A ToolResult is renderable when it succeeded AND its payload
    actually carries content the renderer can talk about.

    The renderer-fail path historically lit up when EVERY result was
    `ok=False` (network errors, etc) — `_friendly_render_error` is the
    right answer there. But Stan's «примеры слова factory» case is
    subtler: `hybrid_search` returned `data={'matches':[], 'lexical_n':0,
    'semantic_n':0}` (ok-True with an empty bag) and the LLM, faced
    with an envelope it couldn't render, dumped the JSON literal back
    into chat. Treat «empty matches» as the same failure class so the
    friendly path catches it too.
    """
    if not r.ok or r.data is None:
        return False
    data = r.data
    if isinstance(data, dict):
        # If the payload declares a collection key, its (non-)emptiness
        # is the verdict — don't let diagnostic scalars mask an empty
        # matches bag. Stan «factory» case: matches=[] with lexical_n=0
        # used to slip past the scalar check below and ship the empty
        # envelope to the LLM.
        for k in _CONTENT_LIST_KEYS:
            if k in data:
                v = data.get(k)
                return isinstance(v, list) and bool(v)
        # Scalar-bearing tools (book_readability, find_book,
        # enrich_word, word_etymology) — any non-metadata, non-empty
        # scalar counts. Use `is`-checks to keep numeric 0 / False
        # from counting as «empty» (a Flesch score of 0 is real).
        for k, v in data.items():
            if k.startswith("_") or k in _METADATA_KEYS:
                continue
            if v is None:
                continue
            if isinstance(v, (str, list, dict, tuple)) and not v:
                continue
            return True
        return False
    # Non-dict payloads (rare; e.g. plain str) — keep if truthy.
    return bool(data)


def _dispatch_render(
    question: str,
    plan: plan_mod.QueryPlan,
    results: list[ToolResult],
    *,
    model: str,
    ollama_host: str,
    history: list[dict] | None = None,
    cancel_event=None,
) -> tuple[str, dict]:
    """Single render path: `_llm_render` (RENDER_PROMPT + free-form LLM).

    W-6 follow-up (2026-05-24) — guard against the «raw JSON in chat»
    failure mode (Stan «примеры слова factory» prod): when no tool
    returned usable rows, short-circuit to `_friendly_render_error`
    instead of pushing the empty/errored payload through the LLM.
    qwen3:14b on a bare error envelope frequently dumped the JSON
    literal back into the answer; the friendly path produces a clean
    Russian sentence + whatever partial data exists.

    Phase 1 (2026-05-22) — the v5 typed renderer / prose-binder branches
    (gated WC_V5_RENDERER / WC_V5_PROSE, never on in prod) were deleted.
    No alternative renderer to dispatch to."""
    if results and not any(_has_useful_data(r) for r in results):
        # Build a synthetic exception so `_friendly_render_error` can
        # pick a human-readable lead. Prefer the first concrete tool
        # error message — falls back to a generic phrase if every
        # result was a bare empty.
        reason = None
        for r in results:
            if r.error and r.error.message:
                reason = f"{r.tool}: {r.error.message}"
                break
        if reason is None:
            reason = ("инструменты вернулись без данных по этому "
                      "запросу")
        err = _ToolPipelineEmpty(reason)
        meta = {"fallback_reason": "tools_returned_no_data"}
        return _friendly_render_error(err, results), meta
    return _llm_render(
        question, plan, results,
        model=model, ollama_host=ollama_host,
        history=history,
        cancel_event=cancel_event,
    )


class RenderCancelled(Exception):
    """Raised inside _llm_render when cancel_event fires (client dropped
    the SSE mid-render). Caught quietly in ask_stream — NOT surfaced to
    the user as an error. Its job is to break the blocking Ollama read
    and close the socket so the single runner slot frees. (S-P2b)"""
    pass


class _ToolPipelineEmpty(Exception):
    """Sentinel exception fed to `_friendly_render_error` when the
    tool pipeline produced no usable rows. The friendly handler reads
    the message to pick a lead line — keep it short and in Russian."""
    pass


# ---------- ask() — non-streaming ----------


def ask(
    question: str,
    history: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    **kwargs,
) -> dict:
    """Run the planner pipeline. Returns a v1-compatible dict.

    {"answer", "tool_calls", "iterations", "model", "elapsed_sec", "intent"}
    """
    t0 = time.perf_counter()
    # v5 Phase 5 — optional envelope (trace + budget). None when flag off.
    _v5_env = _v5_pipeline_envelope(question, engine="v2")

    # Sprint 22+ Round 12 post-deploy — «повтори / repeat» short-circuit.
    # Stan 2026-05-20: «повтори» without other context triggered v4
    # LLM-planner which returned a CLARIFY IN ARABIC because qwen3:14b
    # on bare ultra-short input picks a random target language. The
    # correct behavior is trivial: return the previous assistant turn
    # verbatim. No LLM, no router, no risk of language drift.
    if history_mod.is_repeat_request(question):
        prev = history_mod.last_assistant_message(history)
        if prev:
            return {
                "answer": prev,
                "tool_calls": [],
                "iterations": 0,
                "model": model,
                "elapsed_sec": round(time.perf_counter() - t0, 2),
                "intent": "repeat",
                "intent_confidence": 1.0,
                "via": "repeat-shortcut",
            }
        # No prior — fall through to clarify
        return {
            "answer": "Пока нечего повторить — задай свой первый вопрос.",
            "tool_calls": [],
            "iterations": 0,
            "model": model,
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "intent": "clarify",
            "intent_confidence": 1.0,
            "via": "repeat-no-history",
        }

    # Sprint 20+ — v4 LLM planner takes over for follow-up turns when
    # the feature flag is on. Stan 2026-05-19 («отдать всё v4 для
    # post-followup queries»): rules-based followup logic accumulated
    # 5 different reroute branches (translate / propn-remove / rerank /
    # expand / context-swap), each with whitelist gaps and flag-contract
    # gaps documented in the pipeline trace. LLM sees the prior user
    # message + prior assistant response + tool catalog and composes
    # the right DAG — including enrich_word chains for translation,
    # stricter affinity re-runs for «убери имена», cross-tool combos.
    # When flag is off, the rules path continues exactly as before.
    v4_followup_used = False
    v4_followup_report = None
    if history and history_mod._looks_like_followup(question):
        try:
            from scripts.v2.planner import llm_planner as _llmp
        except ImportError:
            _llmp = None
        if _llmp is not None and _llmp.LLM_PLANNER_ENABLED:
            try:
                pres = _llmp.plan_query(question, history=history)
            except Exception:
                log.exception("v4 LLM planner crashed on followup")
                pres = None
            if pres is not None and pres.ok and pres.plan and pres.plan.steps:
                v4_followup_used = True
                v4_followup_report = pres
                rr_v4 = router_mod.execute(
                    pres.plan,
                    budget=_v5_budget_from_envelope(
                        _v5_env, intent_label=pres.plan.intent_hint),
                )
                # Build stand-in intent + entities so downstream
                # observability + render code paths keep working.
                intent = int_mod.IntentMatch(
                    label=pres.plan.intent_hint or "v4_followup",
                    confidence=0.9,
                    matched_pattern="v4-followup-llm",
                )
                entities = ent_mod.Entities()
                plan = rr_v4.plan
                rr = rr_v4
            elif pres is not None and pres.clarify:
                # Sprint 22+ Round 12 Q11/Q12 fix: v4 LLM-planner returned
                # clarify on followup. BEFORE accepting it, try the v3
                # rules path — translate_word_list and export_word_list
                # have working prior-words extraction from markdown. If
                # v3 produces a non-clarify plan with steps, prefer it.
                # Only fall to v4's clarify when v3 ALSO clarifies.
                v3_intent_label = None
                try:
                    inferred = history_mod.infer_followup_intent(
                        question, history)
                    if inferred and inferred not in ("clarify",):
                        v3_intent_label = inferred
                except Exception:
                    log.exception("v3 followup fallback failed")
                if v3_intent_label:
                    # Build a v3 plan with that intent
                    v3_entities = ent_mod.extract(question)
                    v3_entities = history_mod.merge_with_history(
                        v3_entities, history, question)
                    try:
                        v3_plan = plan_mod.build(v3_intent_label, v3_entities)
                    except Exception:
                        log.exception("v3 plan build failed for %s",
                                      v3_intent_label)
                        v3_plan = None
                    if v3_plan and not v3_plan.needs_clarify and v3_plan.steps:
                        # v3 has a real plan — use it instead of v4 clarify.
                        intent = int_mod.IntentMatch(
                            label=v3_intent_label, confidence=0.7,
                            matched_pattern="v4-clarify-then-v3-rescue",
                        )
                        entities = v3_entities
                        plan = v3_plan
                        v4_followup_report = pres
                    else:
                        intent = int_mod.IntentMatch(
                            label="clarify", confidence=0.5,
                            matched_pattern="v4-followup-clarify",
                        )
                        entities = ent_mod.Entities()
                        plan = plan_mod.QueryPlan(
                            intent="clarify", entities=entities, steps=[],
                            needs_clarify=True,
                            clarify_question=pres.clarify,
                            explain="v4 clarify, v3 also clarified",
                        )
                        v4_followup_report = pres
                else:
                    # v3 has nothing to add → use v4's clarify.
                    intent = int_mod.IntentMatch(
                        label="clarify", confidence=0.5,
                        matched_pattern="v4-followup-clarify",
                    )
                    entities = ent_mod.Entities()
                    plan = plan_mod.QueryPlan(
                        intent="clarify", entities=entities, steps=[],
                        needs_clarify=True,
                        clarify_question=pres.clarify,
                        explain="v4 LLM planner clarify on followup",
                    )
                    v4_followup_report = pres

    llm_used_for = None
    if v4_followup_used or (v4_followup_report is not None
                              and v4_followup_report.clarify is not None):
        # v4 followup path already set intent / entities / plan above.
        # Skip the rules-based pipeline entirely for this turn.
        pass
    else:
        intent = int_mod.classify(question)
        entities = ent_mod.extract(question)
        # Multi-turn backfill: «приведи примеры такого», «эти слова», «у этого
        # автора» reuse the last turn's author/book/word so the planner doesn't
        # clarify on every follow-up.
        entities = history_mod.merge_with_history(entities, history, question)
        # Follow-up intent inference: if the user's phrasing implies a specific
        # intent (e.g. «приведи примеры» → word_contexts), override clarify so
        # the planner can route to a real tool. Only kicks in for clarify-class
        # responses so explicit intents still win.
        if intent.label == "clarify":
            inferred = history_mod.infer_followup_intent(question, history)
            if inferred:
                intent = int_mod.IntentMatch(label=inferred, confidence=0.75,
                                             matched_pattern="followup-inferred")
        # LLM fallback (Sprint 13). Stan's 2026-05-18 demon round 2 hit 50%
        # clarify on free-form Russian; rule-based regex can never close the
        # gap with human phrasing breadth. Ask the local LLM to classify into
        # the 35-label taxonomy when rules + history both miss.
        #
        # Sprint 14 round 2 escalation: not just intent, also entities.
        if intent.label == "clarify":
            try:
                from scripts.v2.planner import llm_intent
                full = llm_intent.classify_and_extract(question, history)
                if full is not None:
                    intent = int_mod.IntentMatch(label=full["intent"],
                                                  confidence=0.7,
                                                  matched_pattern="llm-fallback-full")
                    _merge_llm_entities(entities, full)
                    llm_used_for = "intent+entities"
            except Exception:
                pass
        elif _needs_entity_help(intent.label, entities):
            try:
                from scripts.v2.planner import llm_intent
                full = llm_intent.classify_and_extract(question, history)
                if full is not None:
                    _merge_llm_entities(entities, full)
                    llm_used_for = "entities-only"
            except Exception:
                pass
        plan = plan_mod.build(intent.label, entities)

    # v4 LLM planner — when rules-based path produces clarify AND the
    # planner feature flag is on, try generating a DAG plan with the
    # LLM. This handles compound queries (multi-book / triangulation /
    # etymology-ratio) without hand-coded branches in
    # `_smart_clarify_recipe`. See docs/v2/PLANNER.md §6 (v4).
    #
    # Skip when the followup branch above already ran the LLM planner
    # — otherwise we double-dispatch the same query against Ollama
    # (cache usually hits but still wastes a lookup).
    v4_planner_used = False
    v4_planner_report = None
    # B-R17-1 stage3.2 v3 — when rules-path explicitly emits an
    # authoritative clarify (e.g. ambiguous surname Wells), skip v4
    # LLM planner fallback. Without this guard, v4 sees
    # needs_clarify=True and overrides our clarify with a generic
    # top_books plan.
    _skip_v4_planner = bool(getattr(plan, "authoritative_clarify", False))
    if (plan.needs_clarify and not v4_followup_used
            and v4_followup_report is None
            and not _skip_v4_planner):
        try:
            from scripts.v2.planner import llm_planner as _llmp
        except ImportError:
            _llmp = None
        if _llmp is not None and _llmp.LLM_PLANNER_ENABLED:
            try:
                pres = _llmp.plan_query(question, history=history)
            except Exception:
                log.exception("v4 LLM planner crashed")
                pres = None
            if pres is not None and pres.ok and pres.plan and pres.plan.steps:
                v4_planner_used = True
                v4_planner_report = pres
                rr_v4 = router_mod.execute(
                    pres.plan,
                    budget=_v5_budget_from_envelope(
                        _v5_env, intent_label=pres.plan.intent_hint),
                )
                # Adopt the v4 router result; downstream render/critic
                # treats it like any other ToolResult set.
                plan = rr_v4.plan
                rr = rr_v4
            elif pres is not None and pres.clarify:
                # Sprint 22+ Round 12: when v4 main-planner gives up,
                # try v3 rules (infer_followup_intent) before accepting
                # the v4 clarify. Stan 2026-05-20: «да конкретные
                # произведения, в которых встречается имя Anna» was
                # semantically a followup but lexically slid past
                # _looks_like_followup → v4 main fired without followup
                # context → clarified out. With «да X» now in followup
                # triggers, infer_followup_intent may resolve it here.
                v3_intent_label = None
                try:
                    inferred = history_mod.infer_followup_intent(
                        question, history)
                    if inferred and inferred not in ("clarify",):
                        v3_intent_label = inferred
                except Exception:
                    log.exception("v3 main-planner fallback failed")
                if v3_intent_label:
                    v3_entities = ent_mod.extract(question)
                    v3_entities = history_mod.merge_with_history(
                        v3_entities, history or [], question)
                    try:
                        v3_plan = plan_mod.build(v3_intent_label, v3_entities)
                    except Exception:
                        log.exception("v3 plan build failed (main)")
                        v3_plan = None
                    if v3_plan and not v3_plan.needs_clarify and v3_plan.steps:
                        # v3 has a real plan — use it instead of v4 clarify
                        intent = int_mod.IntentMatch(
                            label=v3_intent_label, confidence=0.7,
                            matched_pattern="v4-main-clarify-then-v3-rescue",
                        )
                        entities = v3_entities
                        plan = v3_plan
                        v4_planner_report = pres
                    else:
                        plan.clarify_question = pres.clarify
                        v4_planner_report = pres
                else:
                    # LLM declined — replace the generic clarify with the
                    # specific question it asked.
                    plan.clarify_question = pres.clarify
                    v4_planner_report = pres

    if plan.needs_clarify and not v4_planner_used:
        clarify_answer = plan.clarify_question or "Уточни запрос."
        # v3.0 Phase A3: detect «unknown author candidate» in the query —
        # capitalized tokens that LOOK like an author surname but weren't
        # matched by AUTHOR_ALIASES. These are the candidates Stan should
        # add to AUTHOR_ALIASES_CURATED OR trigger a rebuild of the
        # generated layer. Surfaces in `/admin/failed` as a separate
        # signal from generic clarifies.
        unknown_authors = _detect_unknown_author_candidates(question, entities)
        # v2.7: log failed query so admin UI can show «what users asked
        # but didn't get a tool answer for». Reason = why the planner
        # bounced out (no author / no book / no word / etc).
        obs_mod.log_request({
            "question_truncated": question[:300],
            "intent": intent.label,
            "intent_confidence": intent.confidence,
            "plan_steps": [],
            "tool_calls": [],
            "total_elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "answer_truncated": clarify_answer[:300],
            "is_failure": True,
            "failure_kind": "clarify",
            "failure_reason": plan.explain or "no specific reason",
            # v4 attempt visibility on clarify-falls — Stan can tell from
            # the log whether a v4 path was tried and gave up, vs never
            # attempted at all.
            "v4_followup_used": v4_followup_used,
            "v4_followup_attempts": (v4_followup_report.attempts
                                      if v4_followup_report else None),
            "v4_followup_elapsed_s": (round(v4_followup_report.elapsed_s, 2)
                                       if v4_followup_report else None),
            "v4_planner_attempted": v4_planner_report is not None,
            "v4_planner_attempts": (v4_planner_report.attempts
                                     if v4_planner_report else None),
            **({"unknown_author_candidates": unknown_authors}
               if unknown_authors else {}),
            **_v5_envelope_extras(_v5_env, intent_label=intent.label),
        })
        return {
            "answer": clarify_answer,
            "tool_calls": [],
            "iterations": 0,
            "model": model,
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "intent": intent.label,
            "intent_confidence": intent.confidence,
        }
    if plan.out_of_scope_reason:
        # v2.7: also log out_of_scope. Stan wants to see these because
        # some are legit refusals (genre_compare, translation_quality)
        # but others are «user phrased it wrong, classifier mis-routed»
        # — both need eyeballs on free-form Russian.
        obs_mod.log_request({
            "question_truncated": question[:300],
            "intent": "out_of_scope",
            "original_intent": intent.label,
            "intent_confidence": intent.confidence,
            "plan_steps": [],
            "tool_calls": [],
            "total_elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "answer_truncated": plan.out_of_scope_reason[:300],
            "is_failure": True,
            "failure_kind": "out_of_scope",
            "failure_reason": plan.explain or plan.out_of_scope_reason[:200],
            **_v5_envelope_extras(_v5_env, intent_label=intent.label),
        })
        return {
            "answer": plan.out_of_scope_reason,
            "tool_calls": [],
            "iterations": 0,
            "model": model,
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            # Surface as out_of_scope so functional runners classify this as
            # an intentional refusal, not a missing answer.
            "intent": "out_of_scope",
            "original_intent": intent.label,
            "intent_confidence": intent.confidence,
        }

    # Skip router if EITHER v4 path already produced results (followup
    # path runs early; main v4 path runs after rules clarify).
    if not v4_planner_used and not v4_followup_used:
        rr = router_mod.execute(
            plan,
            budget=_v5_budget_from_envelope(_v5_env, intent_label=intent.label),
        )

    render_meta: dict = {}
    if intent.label == "introduction":
        # Skip LLM — return the static intro the user expects.
        answer = _intro_text()
    else:
        try:
            answer, render_meta = _dispatch_render(
                question, plan, rr.results,
                model=model, ollama_host=ollama_host,
                history=history,
            )
        except Exception as e:
            log.exception("renderer LLM failed")
            # Sprint 21 B104 — soft network-error fix. Stan: «всё ещё
            # выпадает network error, найти причину, сделать мягкий фикс».
            # Most common cause: Ollama timeout (model overloaded or
            # GPU dropped). Build a user-friendly message that says
            # WHAT happened + the partial tool results so the user
            # gets value even when Ollama is unhealthy.
            answer = _friendly_render_error(e, rr.results)

    # ---- Sprint 6.1: critic pass ----
    # Verify the rendered answer against tool_results. Only annotates when
    # the critic flagged something; silent on a clean answer.
    critic_summary_records = [
        {"tool": r.tool, "ok": r.ok, "data": r.data,
         # E17 (2026-05-22) — include r.query so numeric_audit treats
         # filter values (min_corpus_count=200, top=30, etc) as legitimate
         # numbers. Stan «Dorian Gray ADJ» bug: empty_state said «при
         # min_corpus_count=200» and audit flagged 200 as «not in data»
         # because query field was hidden in the harvest path.
         "query": r.query,
         "coverage": {"books_matched": r.coverage.books_matched,
                      "books_total": r.coverage.books_total},
         "warnings": [{"code": w.code, "message": w.message} for w in r.warnings],
         # E17 — view-side filter values rendered by template_executor's
         # empty_state block («Применённые фильтры: min_corpus_count=200»)
         # also need to count as «in data».
         "_view_filter_values": _harvest_view_filter_values(r.view),
        }
        for r in rr.results
    ]
    verdict = critic_mod.review(
        answer, critic_summary_records, intent=intent.label,
        ollama_host=ollama_host,
    )
    answer = critic_mod.annotate_answer(answer, verdict)

    # Sprint 16 Phase D — programmatic numeric audit. Runs after the
    # critic LLM pass so its footer attaches to the same answer body.
    audit_report = audit_mod.audit_numbers(
        answer, critic_summary_records, intent=intent.label,
    )
    answer = audit_mod.annotate_with_audit(answer, audit_report)

    tool_calls = [
        {"name": r.tool, "args": r.query,
         "result_summary": r.to_llm_string(max_chars=240),
         "ok": r.ok, "runtime_ms": r.runtime_ms}
        for r in rr.results
    ]
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    obs_mod.log_request({
        "question_truncated": question[:300],
        "intent": intent.label,
        "intent_confidence": intent.confidence,
        "plan_steps": [s.tool for s in plan.steps],
        "tool_calls": [
            {"name": r.tool, "runtime_ms": r.runtime_ms,
             "ok": r.ok, "cache_hit": r.cache_hit}
            for r in rr.results
        ],
        "total_elapsed_ms": elapsed_ms,
        "critic_verified": verdict.verified,
        "critic_unsupported_n": len(verdict.unsupported_claims),
        "numeric_audit_mismatches": len(audit_report.mismatches),
        # Sprint 17 — Ollama-side token counts (renderer + critic).
        # Lets the admin dashboard answer «do we need more num_ctx».
        "renderer_prompt_tokens": render_meta.get("prompt_tokens"),
        "renderer_eval_tokens":   render_meta.get("eval_tokens"),
        "critic_prompt_tokens":   verdict.prompt_tokens,
        "critic_eval_tokens":     verdict.eval_tokens,
        # Sprint 18 — retrieval source log. Which pg_ids/snippets
        # actually fed the renderer? Splits «model gave bad answer»
        # from «retrieval gave bad source». None when no RAG tool ran.
        "retrieval_log": _extract_retrieval_log(rr.results),
        # Sprint 19 — user-upload disclosure. True iff any tool result
        # referenced a U-prefixed book id.
        "user_uploads_used":  _detect_user_uploads(rr.results)[0],
        "user_upload_count":  _detect_user_uploads(rr.results)[1],
        # v4 — LLM planner usage + plan details so Stan can review
        # the JSONL log and harvest patterns for promotion to rules.
        "v4_planner_used": v4_planner_used,
        "v4_planner_attempts": (v4_planner_report.attempts
                                 if v4_planner_report else None),
        "v4_planner_elapsed_s": (round(v4_planner_report.elapsed_s, 2)
                                  if v4_planner_report else None),
        # v4 followup path — Stan's Sprint 20+ routing for post-followup
        # queries. Tells the dashboard whether the LLM-planner-first
        # gate fired (separate from the clarify-rescue v4 path above).
        "v4_followup_used": v4_followup_used,
        "v4_followup_attempts": (v4_followup_report.attempts
                                  if v4_followup_report else None),
        "v4_followup_elapsed_s": (round(v4_followup_report.elapsed_s, 2)
                                   if v4_followup_report else None),
        "answer_truncated": answer[:300],
        **_v5_envelope_extras(_v5_env, intent_label=intent.label,
                              render_meta=render_meta),
    })
    return {
        "answer": answer,
        "tool_calls": tool_calls,
        "iterations": len(rr.results),
        "critic": {
            "verified": verdict.verified,
            "issues_flagged": verdict.has_issues(),
            "unsupported_claims_n": len(verdict.unsupported_claims),
            "summary": verdict.summary,
        },
        "model": model,
        "elapsed_sec": round(elapsed_ms / 1000, 2),
        "intent": intent.label,
        "intent_confidence": intent.confidence,
    }


# ---------- ask_stream() — SSE ----------


def ask_stream(
    question: str,
    history: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    cancel_event=None,
    **kwargs,
) -> Iterator[dict]:
    """SSE events compatible with v1 plus v2-specific intent/plan/clarify."""
    if cancel_event is None:
        cancel_event = threading.Event()
    yield {"event": "start"}
    t0 = time.perf_counter()
    # v5 Phase 5 — optional envelope (trace + budget). None when flag off.
    _v5_env = _v5_pipeline_envelope(question, engine="v2")

    # Sprint 22+ Round 12 post-deploy — «повтори / repeat» short-circuit
    # (stream variant). Same defense as ask() — no LLM, no router, no
    # language drift risk. Emit minimal SSE events so the UI flows.
    if history_mod.is_repeat_request(question):
        prev = history_mod.last_assistant_message(history)
        text = prev or "Пока нечего повторить — задай свой первый вопрос."
        yield {"event": "intent", "label": "repeat", "confidence": 1.0,
               "via": "repeat-shortcut"}
        yield {"event": "answer", "text": text}
        yield {"event": "done", "tool_calls": [], "iterations": 0,
               "elapsed_sec": round(time.perf_counter() - t0, 2),
               "intent": "repeat"}
        return

    # Sprint 20+ — v4 LLM planner takes the followup path. See `ask()`
    # for the architectural decision. Mirrors the same gating logic;
    # only difference is event emission order.
    v4_followup_used = False
    v4_followup_report = None
    if history and history_mod._looks_like_followup(question):
        try:
            from scripts.v2.planner import llm_planner as _llmp
        except ImportError:
            _llmp = None
        if _llmp is not None and _llmp.LLM_PLANNER_ENABLED:
            try:
                pres = _llmp.plan_query(question, history=history)
            except Exception:
                log.exception("v4 LLM planner crashed on followup (stream)")
                pres = None
            if pres is not None and pres.ok and pres.plan and pres.plan.steps:
                v4_followup_used = True
                v4_followup_report = pres
                rr_v4 = router_mod.execute(
                    pres.plan,
                    budget=_v5_budget_from_envelope(
                        _v5_env, intent_label=pres.plan.intent_hint),
                )
                intent = int_mod.IntentMatch(
                    label=pres.plan.intent_hint or "v4_followup",
                    confidence=0.9,
                    matched_pattern="v4-followup-llm",
                )
                entities = ent_mod.Entities()
                plan = rr_v4.plan
                results = rr_v4.results
                # Emit v4_plan event + replay step events
                yield {"event": "intent", "label": intent.label,
                       "confidence": intent.confidence,
                       "explain": plan.explain, "via": "v4_followup"}
                yield {"event": "v4_plan",
                       "intent_hint": pres.plan.intent_hint or "v4_followup",
                       "rationale": pres.plan.rationale,
                       "steps": [{"id": s.id, "tool": s.tool,
                                   "args": s.args, "needs": s.needs}
                                  for s in pres.plan.steps],
                       "render_hint": pres.plan.render_hint,
                       "elapsed_s": round(pres.elapsed_s, 2),
                       "attempts": pres.attempts}
                for ev in rr_v4.events:
                    if ev.kind == "step_start":
                        yield {"event": "tool_call", "name": ev.tool,
                               "args": ev.args}
                    elif ev.kind == "step_done" and ev.result is not None:
                        tr = ev.result
                        yield {"event": "tool_result", "name": ev.tool,
                               "ms": tr.runtime_ms, "ok": tr.ok,
                               "summary": tr.to_llm_string(max_chars=240)}
            elif pres is not None and pres.clarify:
                # Sprint 22+ Round 12 Q11/Q12: v3 rules fallback before
                # accepting v4 clarify. Same logic as the non-stream path.
                v3_intent_label = None
                try:
                    inferred = history_mod.infer_followup_intent(
                        question, history)
                    if inferred and inferred not in ("clarify",):
                        v3_intent_label = inferred
                except Exception:
                    log.exception("v3 followup fallback failed (stream)")
                if v3_intent_label:
                    v3_entities = ent_mod.extract(question)
                    v3_entities = history_mod.merge_with_history(
                        v3_entities, history, question)
                    try:
                        v3_plan = plan_mod.build(v3_intent_label, v3_entities)
                    except Exception:
                        log.exception("v3 plan build failed (stream)")
                        v3_plan = None
                    if v3_plan and not v3_plan.needs_clarify and v3_plan.steps:
                        v4_followup_report = pres
                        intent = int_mod.IntentMatch(
                            label=v3_intent_label, confidence=0.7,
                            matched_pattern="v4-clarify-then-v3-rescue",
                        )
                        entities = v3_entities
                        plan = v3_plan
                    else:
                        v4_followup_report = pres
                        intent = int_mod.IntentMatch(
                            label="clarify", confidence=0.5,
                            matched_pattern="v4-followup-clarify",
                        )
                        entities = ent_mod.Entities()
                        plan = plan_mod.QueryPlan(
                            intent="clarify", entities=entities, steps=[],
                            needs_clarify=True,
                            clarify_question=pres.clarify,
                            explain="v4 clarify, v3 also clarified (stream)",
                        )
                else:
                    v4_followup_report = pres
                    intent = int_mod.IntentMatch(
                        label="clarify", confidence=0.5,
                        matched_pattern="v4-followup-clarify",
                    )
                    entities = ent_mod.Entities()
                    plan = plan_mod.QueryPlan(
                        intent="clarify", entities=entities, steps=[],
                        needs_clarify=True,
                        clarify_question=pres.clarify,
                        explain="v4 LLM planner clarify on followup (stream)",
                    )

    if not v4_followup_used and (v4_followup_report is None
                                  or v4_followup_report.clarify is None):
        intent = int_mod.classify(question)
        entities = ent_mod.extract(question)
        entities = history_mod.merge_with_history(entities, history, question)
        if intent.label == "clarify":
            inferred = history_mod.infer_followup_intent(question, history)
            if inferred:
                intent = int_mod.IntentMatch(label=inferred, confidence=0.75,
                                             matched_pattern="followup-inferred")
    # LLM fallback for free-form phrasings (Sprint 13/14). Skip when v4
    # followup already produced something.
    if (not v4_followup_used and v4_followup_report is None
            and intent.label == "clarify"):
        try:
            from scripts.v2.planner import llm_intent
            full = llm_intent.classify_and_extract(question, history)
            if full is not None:
                intent = int_mod.IntentMatch(label=full["intent"],
                                              confidence=0.7,
                                              matched_pattern="llm-fallback-full")
                _merge_llm_entities(entities, full)
        except Exception:
            pass
    elif _needs_entity_help(intent.label, entities):
        try:
            from scripts.v2.planner import llm_intent
            full = llm_intent.classify_and_extract(question, history)
            if full is not None:
                _merge_llm_entities(entities, full)
        except Exception:
            pass
    if not v4_followup_used and (v4_followup_report is None
                                  or v4_followup_report.clarify is None):
        plan = plan_mod.build(intent.label, entities)

    if not v4_followup_used:
        # Don't double-emit «intent» when v4 followup already streamed it
        yield {"event": "intent", "label": intent.label,
               "confidence": intent.confidence, "explain": plan.explain}

    # v4 LLM planner — mirror the rag_v2.ask() wiring for the SSE path.
    # Stan 2026-05-19 discovered v4 wasn't firing on prod because chat-
    # server uses ask_stream, not ask. When the rules-based path produces
    # clarify AND WC_LLM_PLANNER=on, ask the LLM for a DAG plan and
    # stream its execution. Otherwise the original clarify event flows
    # through as before.
    # Skip when followup branch already ran (avoid double Ollama call).
    v4_planner_used = False
    v4_planner_report = None
    # B-R17-1 stage3.2 v3 — mirror the ask() guard: skip v4 LLM planner
    # when rules-path emits authoritative clarify (ambiguous surname).
    _skip_v4_planner = bool(getattr(plan, "authoritative_clarify", False))
    if (plan.needs_clarify and not v4_followup_used
            and v4_followup_report is None
            and not _skip_v4_planner):
        try:
            from scripts.v2.planner import llm_planner as _llmp
        except ImportError:
            _llmp = None
        if _llmp is not None and _llmp.LLM_PLANNER_ENABLED:
            try:
                pres = _llmp.plan_query(question, history=history)
            except Exception:
                log.exception("v4 LLM planner crashed (stream)")
                pres = None
            if pres is not None and pres.ok and pres.plan and pres.plan.steps:
                v4_planner_used = True
                v4_planner_report = pres
                # Execute the DAG once: stream events + collect results
                # for renderer / critic in a single pass (don't double-
                # dispatch via the polymorphic execute_stream + execute).
                rr = router_mod.execute(
                    pres.plan,
                    budget=_v5_budget_from_envelope(
                        _v5_env, intent_label=pres.plan.intent_hint),
                )
                results = rr.results
                plan = rr.plan  # stub QueryPlan with v4 intent label
                yield {"event": "v4_plan",
                       "intent_hint": pres.plan.intent_hint or "v4_llm_plan",
                       "rationale": pres.plan.rationale,
                       "steps": [{"id": s.id, "tool": s.tool,
                                   "args": s.args, "needs": s.needs}
                                  for s in pres.plan.steps],
                       "render_hint": pres.plan.render_hint,
                       "elapsed_s": round(pres.elapsed_s, 2),
                       "attempts": pres.attempts}
                # Replay per-step events so SSE consumers see the
                # familiar tool_call / tool_result shape.
                for ev in rr.events:
                    if ev.kind == "step_start":
                        yield {"event": "tool_call", "name": ev.tool,
                               "args": ev.args}
                    elif ev.kind == "step_done" and ev.result is not None:
                        tr = ev.result
                        yield {"event": "tool_result", "name": ev.tool,
                               "ms": tr.runtime_ms, "ok": tr.ok,
                               "summary": tr.to_llm_string(max_chars=240)}
            elif pres is not None and pres.clarify:
                plan.clarify_question = pres.clarify
                v4_planner_report = pres

    if plan.needs_clarify and not v4_planner_used:
        # Sprint 17 fix: ask_stream() never logged failures, so the
        # chat-UI path was invisible to /admin/failed. Stan's 2026-05-19
        # «что сложнее читать ... или Сон в летнюю ночь?» went here and
        # left no trace. Mirror the ask() failure-logging block so the
        # JSONL log + admin dashboard see streamed clarifies too.
        unknown_authors = _detect_unknown_author_candidates(question, entities)
        clarify_answer = plan.clarify_question or ""
        obs_mod.log_request({
            "question_truncated": question[:300],
            "intent": intent.label,
            "intent_confidence": intent.confidence,
            "plan_steps": [],
            "tool_calls": [],
            "total_elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "answer_truncated": clarify_answer[:300],
            "is_failure": True,
            "failure_kind": "clarify",
            "failure_reason": plan.explain or "no specific reason",
            "via_stream": True,
            "v4_planner_attempted": v4_planner_report is not None,
            "v4_planner_attempts": (v4_planner_report.attempts
                                     if v4_planner_report else None),
            "v4_planner_elapsed_s": (round(v4_planner_report.elapsed_s, 2)
                                      if v4_planner_report else None),
            "v4_followup_used": v4_followup_used,
            "v4_followup_attempts": (v4_followup_report.attempts
                                      if v4_followup_report else None),
            "v4_followup_elapsed_s": (round(v4_followup_report.elapsed_s, 2)
                                       if v4_followup_report else None),
            **({"unknown_author_candidates": unknown_authors}
               if unknown_authors else {}),
            **_v5_envelope_extras(_v5_env, intent_label=intent.label),
        })
        yield {"event": "clarify", "question": plan.clarify_question or ""}
        yield {"event": "answer", "text": plan.clarify_question or ""}
        yield {"event": "done", "tool_calls": [], "iterations": 0,
               "elapsed_sec": round(time.perf_counter() - t0, 2)}
        return
    if plan.out_of_scope_reason:
        obs_mod.log_request({
            "question_truncated": question[:300],
            "intent": "out_of_scope",
            "original_intent": intent.label,
            "intent_confidence": intent.confidence,
            "plan_steps": [],
            "tool_calls": [],
            "total_elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "answer_truncated": plan.out_of_scope_reason[:300],
            "is_failure": True,
            "failure_kind": "out_of_scope",
            "failure_reason": plan.explain or plan.out_of_scope_reason[:200],
            "via_stream": True,
            **_v5_envelope_extras(_v5_env, intent_label=intent.label),
        })
        yield {"event": "out_of_scope", "reason": plan.out_of_scope_reason}
        yield {"event": "answer", "text": plan.out_of_scope_reason}
        yield {"event": "done", "tool_calls": [], "iterations": 0,
               "elapsed_sec": round(time.perf_counter() - t0, 2)}
        return

    # Skip the v3 step-execution loop when EITHER v4 path already ran
    # the DAG — `results` is already populated above.
    if not v4_planner_used and not v4_followup_used:
        yield {"event": "plan", "steps": [{"tool": s.tool, "args": s.args}
                                          for s in plan.steps]}

        results: list[ToolResult] = []
        # Phase 5: derive budget once so every dispatch in this stream
        # path respects the per-request envelope (was unbounded — last
        # known runaway path in the streaming branch).
        _v5_stream_budget = _v5_budget_from_envelope(
            _v5_env, intent_label=intent.label)
        for idx, step in enumerate(plan.steps):
            # use the same injector logic the non-streaming router uses
            args = router_mod._inject(step.args, results, step.depends_on,
                                      step.inject_result_as)
            if cancel_event.is_set():
                return
            yield {"event": "tool_call", "name": step.tool, "args": args}
            from scripts.v2.tool_registry import dispatch
            tr = dispatch(step.tool, args, budget=_v5_stream_budget)
            results.append(tr)
            yield {"event": "tool_result", "name": step.tool,
                   "ms": tr.runtime_ms, "ok": tr.ok,
                   "summary": tr.to_llm_string(max_chars=240)}
            if not tr.ok and not step.optional:
                break

    # Renderer
    render_meta: dict = {}
    if intent.label == "introduction":
        answer = _intro_text()
    else:
        try:
            answer, render_meta = _dispatch_render(
                question, plan, results,
                model=model, ollama_host=ollama_host,
                history=history,
                cancel_event=cancel_event,
            )
        except RenderCancelled:
            log.info("ask_stream: render cancelled (client disconnect) — "
                     "stopping pump cleanly, runner slot freed")
            return
        except Exception as e:
            # Sprint 21 B104 — soft network-error fix in streaming path.
            log.exception("renderer LLM failed (stream)")
            answer = _friendly_render_error(e, results)
            # Also yield a structured error event so the UI can render a
            # banner instead of just an in-message error. Client sees this
            # BEFORE the final `answer` event lands.
            yield {"event": "error", "kind": "renderer",
                   "message": _short_render_error_message(e)}

    # S-P2b — client vanished during render? don't spend the slot on
    # critic/audit for an answer nobody will read.
    if cancel_event.is_set():
        return

    # Critic pass — same logic as ask() above. We emit the verdict as its
    # own SSE event so the UI can show a confidence badge before the final
    # text lands.
    critic_records = [
        {"tool": r.tool, "ok": r.ok, "data": r.data,
         "coverage": {"books_matched": r.coverage.books_matched,
                      "books_total": r.coverage.books_total},
         "warnings": [{"code": w.code, "message": w.message} for w in r.warnings]}
        for r in results
    ]
    verdict = critic_mod.review(answer, critic_records,
                                intent=intent.label, ollama_host=ollama_host)
    answer = critic_mod.annotate_answer(answer, verdict)

    # Sprint 16 Phase D — numeric audit (programmatic; no LLM call).
    audit_report = audit_mod.audit_numbers(
        answer, critic_records, intent=intent.label,
    )
    answer = audit_mod.annotate_with_audit(answer, audit_report)

    # Sprint 17 fix: mirror ask()'s success-path log_request so the
    # status dashboard sees stream queries (was previously stream-blind).
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    obs_mod.log_request({
        "question_truncated": question[:300],
        "intent": intent.label,
        "intent_confidence": intent.confidence,
        "plan_steps": [s.tool for s in plan.steps],
        "tool_calls": [
            {"name": r.tool, "runtime_ms": r.runtime_ms,
             "ok": r.ok, "cache_hit": r.cache_hit}
            for r in results
        ],
        "total_elapsed_ms": elapsed_ms,
        "critic_verified": verdict.verified,
        "critic_unsupported_n": len(verdict.unsupported_claims),
        "numeric_audit_mismatches": len(audit_report.mismatches),
        # Sprint 17 — Ollama token counts (renderer + critic).
        "renderer_prompt_tokens": render_meta.get("prompt_tokens"),
        "renderer_eval_tokens":   render_meta.get("eval_tokens"),
        "critic_prompt_tokens":   verdict.prompt_tokens,
        "critic_eval_tokens":     verdict.eval_tokens,
        # Sprint 18 — retrieval source log
        "retrieval_log": _extract_retrieval_log(results),
        # Sprint 19 — user-upload disclosure
        "user_uploads_used":  _detect_user_uploads(results)[0],
        "user_upload_count":  _detect_user_uploads(results)[1],
        # v4 — LLM planner usage on the streaming path
        "v4_planner_used": v4_planner_used,
        "v4_planner_attempts": (v4_planner_report.attempts
                                 if v4_planner_report else None),
        "v4_planner_elapsed_s": (round(v4_planner_report.elapsed_s, 2)
                                  if v4_planner_report else None),
        # v4 followup path (Sprint 20+)
        "v4_followup_used": v4_followup_used,
        "v4_followup_attempts": (v4_followup_report.attempts
                                  if v4_followup_report else None),
        "v4_followup_elapsed_s": (round(v4_followup_report.elapsed_s, 2)
                                   if v4_followup_report else None),
        "answer_truncated": answer[:300],
        "via_stream": True,
        **_v5_envelope_extras(_v5_env, intent_label=intent.label,
                              render_meta=render_meta),
    })

    yield {"event": "critic",
           "verified": verdict.verified,
           "issues_flagged": verdict.has_issues(),
           "unsupported_claims_n": len(verdict.unsupported_claims),
           "numeric_audit_mismatches": len(audit_report.mismatches),
           "summary": verdict.summary}
    yield {"event": "answer", "text": answer}
    yield {"event": "done", "tool_calls":
           [{"name": r.tool, "args": r.query, "ok": r.ok,
             "result_summary": r.to_llm_string(max_chars=240)}
            for r in results],
           "iterations": len(results),
           "elapsed_sec": round(time.perf_counter() - t0, 2),
           "intent": intent.label}


# ---------- introduction text (no LLM) ----------


# Sprint 21 B104 — soft network-error fixes. The renderer LLM (qwen3:14b
# via Ollama) can die for a few reasons:
#   - GPU dropped (Stan's RTX 3090 is hot-deck shared with other services
#     and Ollama sometimes loses the device after `docker network` events;
#     we have a cron-watcher but there's a window)
#   - Ollama timeout on heavy num_ctx
#   - ConnectionError if Ollama container restarted
# Pre-Sprint-21 we surfaced `[renderer error: <stacktrace>]` directly in
# the answer. That UX is hostile — user sees a Python repr.
# Now we produce a short friendly message + dump the tool results so the
# user at least sees the data, can copy-paste it, and try again later.

def _short_render_error_message(err: Exception) -> str:
    """Translate a renderer exception into a human-readable one-liner
    suitable for an SSE `error` event."""
    name = type(err).__name__
    # W-6 follow-up (2026-05-24) — tool-pipeline returned nothing
    # usable. The synthetic _ToolPipelineEmpty is raised by
    # `_dispatch_render` before the LLM gets a chance to dump raw
    # JSON. Surface a friendly Russian phrasing instead.
    if isinstance(err, _ToolPipelineEmpty):
        return ("По этому запросу инструменты не вернули данных — "
                "возможно, слово/автор не нашлись в корпусе, либо "
                "сервис поиска не отозвался. Попробуй переформулировать "
                "или сузить запрос.")
    # Specific friendly variants
    msg = str(err).lower()
    if "timeout" in msg or "timed out" in msg or "readtimeout" in msg:
        return ("Ollama не ответил вовремя (модель перегружена или GPU "
                "переключился). Попробуй ещё раз через минуту.")
    if "connection" in msg or "connectionerror" in msg or "refused" in msg:
        return ("Ollama недоступен (контейнер перезапустился или сеть). "
                "Подожди 30 секунд и попробуй ещё раз.")
    if "gpu" in msg or "cuda" in msg or "out of memory" in msg:
        return ("GPU не отдала ответ — возможно перегружен или "
                "перезапущен. Попробуй через минуту.")
    # Generic fallback
    return f"Сбой рендерера ({name}). Подробности в логах. Попробуй ещё раз."


def _friendly_render_error(err: Exception, results: list[ToolResult]) -> str:
    """Build a user-facing answer string when the renderer LLM fails OR
    when the tool pipeline returned nothing usable.

    W-6 follow-up (2026-05-24) — previous implementation called
    `to_llm_string()` on each result and pasted JSON straight into the
    chat. That IS the «raw JSON in chat» bug Stan reported
    («примеры слова factory» 2026-05-22). The render path is for
    end users, not log inspection. Now: short Russian lead + a tight,
    tool-aware bullet list (counts / warnings / error.message), with
    NO JSON dump regardless of payload shape.
    """
    short = _short_render_error_message(err)
    lines: list[str] = [f"⚠️ **{short}**", ""]
    if not results:
        # Nothing else to say — but keep the historical «инструменты тоже
        # не дали» line so callers (test_alpha3_pre_prod_fixes) and users
        # see explicit acknowledgement that no fallback data is available.
        lines.append("К сожалению, инструменты тоже не дали полезных "
                     "данных. Возможно, запрос невалидный — "
                     "попробуй переформулировать.")
        return "\n".join(lines).strip()

    ok_with_data = [r for r in results if _has_useful_data(r)]
    if ok_with_data:
        lines.append("Я успел вызвать инструменты ниже — данные собраны, "
                     "но сформулировать связный ответ не получилось:")
        lines.append("")
        for r in ok_with_data[:5]:
            lines.append(f"- **{r.tool}** — {_tool_data_brief(r)}")
        lines.append("")
        lines.append("_Попробуй задать вопрос ещё раз — обычно второй прогон "
                     "проходит чище._")
        return "\n".join(lines).strip()

    # No usable data — surface the concrete tool-side reason if any.
    failed = [r for r in results if not r.ok]
    if failed:
        lines.append("Что произошло по инструментам:")
        lines.append("")
        for r in failed[:5]:
            reason = (r.error.message if r.error and r.error.message
                      else "вернулся без данных")
            # Trim to one short clause; never JSON-dump the details.
            reason_short = str(reason).strip().splitlines()[0][:200]
            lines.append(f"- **{r.tool}** — {reason_short}")
        lines.append("")
    lines.append("Попробуй переформулировать запрос или сузить его.")
    return "\n".join(lines).strip()


def _tool_data_brief(r: ToolResult) -> str:
    """One-line plain-Russian summary of what a tool result carries.

    Used by `_friendly_render_error` to mention partial successes
    without leaking JSON. Looks at common list-of-rows keys and reports
    a count; falls back to a count of populated top-level fields.
    """
    data = r.data if isinstance(r.data, dict) else None
    if data is None:
        return "данные есть, но в нестандартной форме"
    for key, label in (
        ("matches", "найдено совпадений"),
        ("samples", "сниппетов"),
        ("top", "записей в топе"),
        ("rows", "строк"),
        ("results", "результатов"),
        ("candidates", "кандидатов"),
        ("items", "элементов"),
        ("series", "точек ряда"),
        ("timeline", "точек таймлайна"),
        ("top_collocates", "коллокатов"),
    ):
        v = data.get(key)
        if isinstance(v, list):
            return f"{label}: {len(v)}"
    # Scalar-payload tools (enrich_word, find_book, book_readability) —
    # name a couple of present fields.
    present = [k for k, v in data.items()
               if not k.startswith("_")
               and v not in (None, "", [], {})]
    if present:
        return f"поля: {', '.join(present[:6])}"
    return "данные есть, но пустые"


_INTRO_PATH = Path(__file__).parent / "intro_text.md"


def _intro_text() -> str:
    if _INTRO_PATH.exists():
        return _INTRO_PATH.read_text(encoding="utf-8")
    # Sprint 19+ — meta-question rules route «что за сервис / для кого
    # / бесплатно / как работает» here. The intro now answers all four
    # explicitly: what it does, who benefits, pricing, how it works.
    return (
        f"Меня зовут {ASSISTANT_NAME}. Я **литературный аналитик корпуса "
        f"Project Gutenberg** — ~55 тыс. книг английской литературы, "
        f"проиндексированных семантически (ChromaDB) и лексически (FTS5).\n\n"
        "**Для кого:** исследователи, преподаватели, переводчики, "
        "редакторы, B2-C1 студенты английского, любители стилометрии. "
        "Любой кто хочет ответы на вопросы про язык/стиль/частоту/"
        "эмоции/этимологию в конкретных книгах и у конкретных авторов "
        "— а не пересказ Википедии.\n\n"
        "**Бесплатно:** да, это частный исследовательский проект — "
        "доступ через Basic Auth, без подписок и tracking.\n\n"
        "**Как работает:** детерминированный pipeline (intent classifier "
        "→ entity extractor → plan builder → tool router → renderer "
        "→ critic → numeric audit). Никаких agentic-loop галлюцинаций — "
        "путь от запроса до ответа предсказуем и читается из логов.\n\n"
        "**Что умею:**\n\n"
        "📊 **Стилометрия:** фирменные слова автора (`affinity_by_author`), "
        "сравнение авторов, биграммы, лексическая разнообразность, "
        "Burrows Delta attribution и influences.\n\n"
        "📚 **Книги:** уровень сложности (Flesch+CEFR), архаизмы, "
        "эмоциональный профиль, фирменные слова книги, кто похожие — "
        "по жанру / стилю / теме.\n\n"
        "🔤 **Слова:** контексты, collocates с PMI/NPMI/Dice, "
        "timeline по эпохам, polysemy, этимология через Wiktionary.\n\n"
        "🎓 **Изучение:** vocab B1/B2/C1/rare, enrichment с переводом, "
        "Anki / Markdown / JSON export.\n\n"
        "🌐 **Корпус:** прогресс индексации, топ-авторы, топ-книги, "
        "семантический поиск книг по теме.\n\n"
        "**Примеры запросов:**\n"
        "• «характерные прилагательные в \"The Picture of Dorian Gray\"»\n"
        "• «эмоциональный профиль Frankenstein»\n"
        "• «найди книгу про викторианский Лондон»\n"
        "• «угадай автора отрывка \"the fog came pouring in...\"»\n"
        "• «20 слов B2 из Pride and Prejudice»\n\n"
        "Спрашивай в свободной форме — поймаю смысл и подскажу как "
        "лучше сформулировать если интент непонятен."
    )
