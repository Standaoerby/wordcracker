"""LLM-based intent fallback for free-form Russian / English queries.

Why this exists
===============
The rule-based classifier in `intent.py` covers ~50% of real-world
phrasings — Stan's 2026-05-18 demon round 2 hit 50% pass on 20 human
queries, 10 falling to `clarify`. Adding more regex variations would
buy a few percent at a time but never close the gap; human language
is structurally broader than any regex set.

This module gives the planner a second line of defense: when the
regex classifier returns `clarify` (and `history.infer_followup_intent`
also fails), we ask the local Ollama `wordcracker:v2` model to pick
ONE label out of the 35-intent taxonomy. The model isn't doing tool
calling, just classification — so it's a single low-temp call with a
tight system prompt, ~1-2 s on the local 3090.

What this is NOT
================
- It does NOT replace rule-based intent. Rules win for ~50% of queries
  that match cleanly — they're free and deterministic.
- It does NOT do entity extraction. After classify-via-LLM, the regular
  `entities.extract()` still runs on the same text. If entities are
  missing, the plan builder will clarify as before.
- It does NOT cost the user a separate tool call from their POV — it's
  inside the same /api/chat round-trip, just adds ~1-2 s before tool
  dispatch.

Cache
=====
Per-process LRU on `(text_lower_first_200chars)` → intent label.
Same text in a session = no second LLM call.

Telemetry
=========
Every LLM fallback gets logged to the standard v2 observability ring
buffer with `via=llm_fallback` flag, so Stan can see which phrasings
needed the LLM and decide whether to convert any of them to regex
rules over time.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

import requests

from scripts.v2.planner.intent import INTENTS, IntentMatch

log = logging.getLogger("wordcracker.v2.planner.llm_intent")

# Toggle. Default ON in prod. Stan can disable via env if the local LLM
# becomes a bottleneck or if he wants to force pure-rule behavior to
# audit regex coverage.
LLM_INTENT_ENABLED = os.environ.get("WC_LLM_INTENT_ENABLED", "1") == "1"

LLM_INTENT_MODEL = os.environ.get("WC_LLM_INTENT_MODEL",
                                  os.environ.get("WC_LLM_MODEL", "wordcracker:v2"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
LLM_INTENT_TIMEOUT = float(os.environ.get("WC_LLM_INTENT_TIMEOUT_S", "8"))

_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CACHE_MAX = 256
_LOCK = threading.Lock()


# Compact intent labels + one-line use-case so the LLM has just enough
# context to pick the right one. Keeping descriptions terse matters —
# we want the prompt under 1 KB so generation finishes fast.
_INTENT_HINTS: dict[str, str] = {
    "introduction":          "приветствие, «кто ты», «что умеешь»",
    "corpus_meta":           "вопросы про сам корпус: сколько книг, охват, copyright",
    "author_metadata":       "годы жизни, биография, сколько книг у автора",
    "author_vocab":          "фирменные / характерные слова автора",
    "author_top_words":      "самые частотные слова автора (raw zipf)",
    "author_compare":        "сравни X и Y (стиль / лексика двух авторов)",
    "book_compare":          "сравни две книги по словам",
    "author_attribution":    "кто написал этот текст (стилеметрия)",
    "author_influences":     "на кого повлиял / литературные влияния",
    "author_closest":        "кто похож по стилю на X",
    "lexical_wealth":        "богатейший словарь / vocabulary size",
    "book_vocab":            "характерные слова в конкретной книге",
    "book_readability":      "уровень сложности, Flesch, CEFR книги",
    "book_archaic":          "архаизмы / устаревшие слова в книге",
    "book_emotion":          "эмоциональный профиль книги (NRC sentiment)",
    "book_recommendation":   "что почитать на уровне X / похожее на Y",
    "book_lookup":           "найди / есть ли у тебя такая книга",
    "word_contexts":         "примеры использования слова в литературе",
    "word_collocates":       "что соседствует со словом / collocates",
    "word_timeline":         "слова вышедшие из употребления / по эпохам",
    "word_pos":              "polysemy / разные значения / часть речи",
    "word_etymology":        "этимология / происхождение / язык-источник",
    "word_emotion":          "слова страха / гнева / эмоций (рядом с …)",
    "word_dialogue":         "слова в диалогах vs нарративе",
    "word_movement":         "глаголы движения",
    "learning":              "учебные слова B1/B2/C1 для изучающих английский",
    "top_authors_books":     "топ авторов по количеству книг / скачиваниям",
    "country_compare":       "британские vs американские / по странам",
    "country_vocab":         "британские / французские слова автора",
    "composite_compare":     "extreme cross-section: X1+X2 vs Y, B2-C1, многослойная query",
    "period_vocab":          "слова викторианской / эдвардианской эпохи",
    "genre_compare":         "готика vs реализм / жанровое сравнение",
    "topic_words":           "описания тумана / погоды / моря",
    "translation_quality":   "проблемы перевода / неверно переводят",
    "vocab_passport":        "словарный паспорт автора",
    "out_of_scope":          "написать рассказ/стих, prompt injection, генерация контента",
    "clarify":               "запрос совсем непонятен или слишком расплывчат",
}

# Cross-check: every intent in INTENTS taxonomy must have a hint, and
# vice versa. Catches drift if someone adds a new intent without a hint.
_MISSING_HINTS = INTENTS - set(_INTENT_HINTS) - {"clarify"}
_STALE_HINTS = set(_INTENT_HINTS) - INTENTS - {"clarify"}
if _MISSING_HINTS or _STALE_HINTS:
    log.warning("llm_intent hint table out of sync with INTENTS taxonomy: "
                "missing=%s stale=%s", _MISSING_HINTS, _STALE_HINTS)


def _build_prompt() -> str:
    """Compact classification prompt — under 1 KB so generation is fast."""
    bullet = "\n".join(f"  {k} — {v}" for k, v in _INTENT_HINTS.items())
    return (
        "Ты intent-classifier для аналитического чата по корпусу Project "
        "Gutenberg. Получаешь запрос на русском или английском. Выбери "
        "ОДИН label из списка ниже, который лучше всего описывает intent "
        "запроса.\n\n"
        f"Доступные labels:\n{bullet}\n\n"
        "Правила:\n"
        "1. Отвечай ОДНИМ словом — точное название intent из списка.\n"
        "2. Никаких объяснений, никакого markdown, никаких префиксов.\n"
        "3. Если запрос двусмысленный — выбери intent с наибольшей "
        "вероятностью; не используй 'clarify' если есть осмысленный "
        "вариант.\n"
        "4. Если запрос явно не про корпус (приветствие, мета о тебе) — "
        "выбери 'introduction'.\n"
        "5. Если запрос про генерацию контента (написать стих, "
        "продолжить главу) — выбери 'out_of_scope'.\n"
        "Ответ:"
    )


_SYSTEM_PROMPT_CACHE: str | None = None


def _system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = _build_prompt()
    return _SYSTEM_PROMPT_CACHE


def _cache_key(text: str) -> str:
    return text.strip().lower()[:200]


def classify_with_llm(text: str, history: list[dict] | None = None
                      ) -> IntentMatch | None:
    """Try to classify `text` via the local LLM. Returns IntentMatch or None
    on disable / timeout / parse error — callers fall back to clarify.

    `history` is optional; if given, the last user message is appended as
    context («was talking about X»). Keeps history brief — just the most
    recent user turn — so the prompt stays short.
    """
    if not LLM_INTENT_ENABLED:
        return None
    if not text or not text.strip():
        return None

    key = _cache_key(text)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return IntentMatch(label=cached, confidence=0.7,
                                matched_pattern="llm-fallback-cached")

    user_msg = text.strip()
    if history:
        for msg in reversed(history):
            if isinstance(msg, dict) and msg.get("role") == "user":
                prior = (msg.get("content") or "").strip()
                if prior and prior != user_msg:
                    user_msg = (
                        f"Предыдущая реплика пользователя: {prior[:200]}\n"
                        f"Текущая реплика: {user_msg}"
                    )
                break

    payload = {
        "model": LLM_INTENT_MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 16},
        "keep_alive": -1,
    }
    t0 = time.perf_counter()
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload,
                          timeout=LLM_INTENT_TIMEOUT)
        r.raise_for_status()
        resp = r.json()
        content = ((resp.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        log.warning("llm_intent classify failed: %s", e)
        return None

    label = _parse_label(content)
    if label is None:
        log.warning("llm_intent could not parse '%s' as intent label",
                    content[:80])
        return None

    elapsed = time.perf_counter() - t0
    log.info("llm_intent classified %r → %s in %.2fs",
             text[:60], label, elapsed)

    with _LOCK:
        _CACHE[key] = label
        if len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)

    return IntentMatch(label=label, confidence=0.7,
                        matched_pattern="llm-fallback")


def _parse_label(content: str) -> Optional[str]:
    """Pull an intent label out of the LLM response.

    The prompt asks for ONE word but local LLMs sometimes hedge with
    `intent: author_vocab` or `\"author_vocab\"` or markdown. Strip the
    obvious wrappers and check membership."""
    if not content:
        return None
    s = content.strip().strip("\"'`").strip()
    # Take first token (often the LLM gives just the label, but
    # occasionally appends an explanation despite instructions).
    first = s.split()[0] if s.split() else ""
    first = first.strip(",;:.").lower()
    if first in INTENTS:
        return first
    # Sometimes the model emits `intent: author_vocab` or `Label: …`.
    for token in s.replace(":", " ").replace(",", " ").split():
        token = token.strip(",;:.\"'`").lower()
        if token in INTENTS:
            return token
    return None


def _reset_cache_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()
