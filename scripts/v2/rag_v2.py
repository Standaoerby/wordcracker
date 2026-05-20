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

Tool trace тебе передан как JSON. Каждая запись — {{tool, query, data, warnings, coverage}}. Возьми оттуда factual content. **Ничего вне этих данных.**"""


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


def _llm_render(question: str, plan: plan_mod.QueryPlan,
                results: list[ToolResult], *, model: str,
                ollama_host: str,
                history: list[dict] | None = None) -> tuple[str, dict]:
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
    messages = [
        {"role": "system", "content": RENDER_PROMPT.format(name=ASSISTANT_NAME)},
        {"role": "user", "content": question},
        {"role": "user",
         "content": "Tool data:\n```json\n"
                    + json.dumps(summary_payload, ensure_ascii=False, default=str)
                    + "\n```"},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.3},
        "think": False,
    }
    resp = requests.post(f"{ollama_host}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()
    body = resp.json() or {}
    text = (body.get("message", {}) or {}).get("content", "").strip()
    meta = {
        "prompt_tokens": body.get("prompt_eval_count"),
        "eval_tokens":   body.get("eval_count"),
        "total_duration_ns": body.get("total_duration"),
        "load_duration_ns":  body.get("load_duration"),
    }
    return text, meta


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
                rr_v4 = router_mod.execute_spec(pres.plan)
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
                # LLM produced an honest clarify after seeing the
                # conversation — better than rules clarify.
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
    if plan.needs_clarify and not v4_followup_used and v4_followup_report is None:
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
                rr_v4 = router_mod.execute_spec(pres.plan)
                # Adopt the v4 router result; downstream render/critic
                # treats it like any other ToolResult set.
                plan = rr_v4.plan
                rr = rr_v4
            elif pres is not None and pres.clarify:
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
        rr = router_mod.execute(plan)

    render_meta: dict = {}
    if intent.label == "introduction":
        # Skip LLM — return the static intro the user expects.
        answer = _intro_text()
    else:
        try:
            answer, render_meta = _llm_render(
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
         "coverage": {"books_matched": r.coverage.books_matched,
                      "books_total": r.coverage.books_total},
         "warnings": [{"code": w.code, "message": w.message} for w in r.warnings]}
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
    **kwargs,
) -> Iterator[dict]:
    """SSE events compatible with v1 plus v2-specific intent/plan/clarify."""
    yield {"event": "start"}
    t0 = time.perf_counter()

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
                rr_v4 = router_mod.execute_spec(pres.plan)
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
    if (plan.needs_clarify and not v4_followup_used
            and v4_followup_report is None):
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
                # dispatch via execute_spec_stream + execute_spec).
                rr = router_mod.execute_spec(pres.plan)
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
        for idx, step in enumerate(plan.steps):
            # use the same injector logic the non-streaming router uses
            args = router_mod._inject(step.args, results, step.depends_on,
                                      step.inject_result_as)
            yield {"event": "tool_call", "name": step.tool, "args": args}
            from scripts.v2.legacy_dispatch import dispatch_any
            tr = dispatch_any(step.tool, args)
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
            answer, render_meta = _llm_render(
                question, plan, results,
                model=model, ollama_host=ollama_host,
                history=history,
            )
        except Exception as e:
            # Sprint 21 B104 — soft network-error fix in streaming path.
            log.exception("renderer LLM failed (stream)")
            answer = _friendly_render_error(e, results)
            # Also yield a structured error event so the UI can render a
            # banner instead of just an in-message error. Client sees this
            # BEFORE the final `answer` event lands.
            yield {"event": "error", "kind": "renderer",
                   "message": _short_render_error_message(e)}

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
    """Build a user-facing answer string when the renderer LLM fails.

    Surfaces a friendly Russian message + a summary of the tool results
    that DID succeed, so the user sees value even when Ollama is down.
    """
    short = _short_render_error_message(err)
    lines: list[str] = [f"⚠️ **{short}**", ""]
    ok_results = [r for r in results if r.ok and r.data]
    if ok_results:
        lines.append("Я успел вызвать инструменты ниже — данные есть, но "
                     "сформулировать связный ответ не получилось:")
        lines.append("")
        for r in ok_results[:5]:  # cap at 5 to keep output tractable
            summary = r.to_llm_string(max_chars=400)
            lines.append(f"**{r.tool}** — {summary}")
            lines.append("")
    else:
        lines.append("К сожалению, инструменты тоже не дали полезных "
                     "данных. Возможно, запрос невалидный — "
                     "попробуй переформулировать.")
    return "\n".join(lines).strip()


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
