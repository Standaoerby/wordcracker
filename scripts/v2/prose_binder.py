"""ProseBinder — Phase 3 step B of v5 renderer.

Architecture ([[architecture_refactor_v5_plan]] §P2):

    template_executor (deterministic)
            ↓
    skeleton markdown
            ↓
    ProseBinder (narrow LLM)
            ↓
    intro + next-step prose
            ↓
    verify against view.payload
            ↓
    if any number / entity in prose NOT in payload → drop prose
            ↓
    final answer = [intro] + skeleton + [next_steps]

The structural anti-fabrication contract:

  - LLM generates ONLY intro + next-step suggestions (1-3 sentences each).
  - LLM is given the rendered skeleton and the view payload as closed
    world — instructed "do not introduce new numbers, names, words".
  - Before output, prose is audited:
      * Every number in prose must appear in skeleton or payload.
      * Every author/book/word name in prose must appear in skeleton
        or payload (case-insensitive).
  - If audit fails, prose is dropped silently. Final answer = skeleton
    alone (deterministic, complete, just no narrative glue).

The LLM call is abstracted behind a `LLMCallable` protocol so tests can
mock without touching Ollama. Production wiring uses the same Ollama
host as the existing renderer.

Cost: ~1.5-3s per call on qwen3:14b with hard 8s cap. Falls back to
no-prose on timeout — never blocks the answer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal, Protocol

from scripts.v2.view_types import RenderableView

log = logging.getLogger("wordcracker.v2.prose_binder")


# =====================================================================
# Public contract
# =====================================================================

class LLMCallable(Protocol):
    """A callable that takes (system_prompt, user_prompt, **kwargs) and
    returns a string. Tests inject fakes; production injects an Ollama
    wrapper.
    """
    def __call__(self, system_prompt: str, user_prompt: str,
                  /, *, timeout_s: float = 8.0) -> str: ...


@dataclass
class ProseResult:
    """Outcome of ProseBinder.bind. `intro` and `next_steps` are
    populated only if verification passes.

    Fields:
      intro              — short narrative paragraph (None on drop)
      next_steps         — list of next-step suggestion strings
      used_llm           — whether LLM was actually called
      verification_passed— True if prose survived audit, False if dropped
      verification_failures — list of human-readable reasons for drop
      llm_elapsed_s      — wall-clock time on LLM call
      tokens_total       — token usage (best-effort, None if unknown)
    """
    intro: str | None = None
    next_steps: list[str] = field(default_factory=list)
    used_llm: bool = False
    verification_passed: bool = True
    verification_failures: list[str] = field(default_factory=list)
    llm_elapsed_s: float = 0.0
    tokens_total: int | None = None

    def to_markdown(self) -> str:
        parts = []
        if self.intro:
            parts.append(self.intro.strip())
        if self.next_steps:
            parts.append("")
            parts.append("**Что ещё можно спросить:**")
            for q in self.next_steps:
                parts.append(f"- {q.strip()}")
        return "\n".join(parts).strip()


# =====================================================================
# Default prompt — short, locked-domain
# =====================================================================

_SYSTEM_PROMPT_RU = """Тебя зовут Словоёб — литературный аналитик корпуса Project Gutenberg.

Тебе пришёл готовый ответ (markdown-таблица или текст с фактами от детерминированного инструмента) и исходный вопрос пользователя.

Твоя задача — написать ТОЛЬКО короткое intro (1-2 предложения, до 250 символов) и 1-2 next-step вопроса (что ещё можно спросить).

⚠️ КРИТИЧНО:
1. ЗАПРЕЩЕНО упоминать факты, числа, имена, слова, которых НЕТ в готовом ответе или в payload. Любое такое упоминание = твой ответ будет отброшен audit-слоем.
2. ЗАПРЕЩЕНО переписывать или пересказывать таблицу — она уже отрендерена.
3. ЗАПРЕЩЕНО предлагать вызвать новый tool — это закрытый мир.
4. Язык intro и next-steps = язык вопроса пользователя.

Формат ответа: ровно один JSON-объект, без markdown-обёртки, без комментариев:

{"intro": "...", "next_steps": ["...", "..."]}

Если intro невозможно — оставь intro:"" и предложи 1-2 next-step вопроса.
Если ничего не приходит в голову — верни {"intro":"", "next_steps":[]}."""


_SYSTEM_PROMPT_EN = """You are Wordcracker — a literary analyst of the Project Gutenberg corpus.

You receive a ready-made answer (markdown table or text with facts from a deterministic tool) and the original user question.

Your task is ONLY to write a short intro (1-2 sentences, max 250 chars) and 1-2 next-step questions.

⚠️ HARD CONSTRAINTS:
1. NEVER mention facts, numbers, names, words not in the answer or payload. Audit will drop your output otherwise.
2. NEVER rewrite or paraphrase the table — it is already rendered.
3. NEVER suggest invoking another tool — closed world.
4. Match the user's language.

Output exactly one JSON object, no markdown wrapping, no commentary:

{"intro": "...", "next_steps": ["...", "..."]}

If no intro fits — leave intro:"" and propose 1-2 next-step questions.
If nothing comes to mind — return {"intro":"", "next_steps":[]}."""


def _build_user_prompt(*, question: str, skeleton: str,
                        payload_summary: str, language: str) -> str:
    return (
        f"# Вопрос пользователя\n{question}\n\n"
        f"# Готовый ответ (от детерминированного шаблона)\n{skeleton}\n\n"
        f"# Payload (закрытый мир, единственный источник фактов)\n{payload_summary}\n\n"
        f"# Язык вывода: {language}\n\n"
        f"Верни ОДИН JSON-объект."
    )


def _compact_payload(view: RenderableView, *, max_chars: int = 2000) -> str:
    """Compact JSON dump of view.payload — fed to LLM as the closed
    world it must not exceed."""
    try:
        d = view.to_dict()
        s = json.dumps(d.get("payload", {}), ensure_ascii=False,
                        default=str)
    except Exception:
        s = str(view.payload)[:max_chars]
    if len(s) > max_chars:
        s = s[:max_chars] + '..."(truncated)"'
    return s


# =====================================================================
# Verification — number + entity audit
# =====================================================================

# Matches integers, decimals, percentages, scaled (5k, 2M, 1.5K).
# Conservative: also catches PG ids, dates, scores.
_NUMBER_RE = re.compile(
    r"(?<![A-Za-zА-Яа-я])"
    r"(?:[+-]?\d{1,3}(?:[,\s ]\d{3})+|[+-]?\d+\.\d+|[+-]?\d+)"
    r"(?:\s?[KkMmGg])?"
    r"(?:\s?%)?"
    r"(?![A-Za-zА-Яа-я])"
)


def _extract_numbers_normalized(text: str) -> set[str]:
    """Extract numeric tokens from prose, normalize to compact form for
    comparison. '1 234' / '1,234' / '1234' all become '1234'."""
    out: set[str] = set()
    for m in _NUMBER_RE.finditer(text):
        token = m.group(0)
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            out.add(digits)
            # Also keep the float form if it has a dot
            if "." in token:
                out.add(token.replace(",", ".").replace(" ", "").strip("+-"))
    return out


def _extract_proper_nouns(text: str) -> set[str]:
    """Extract capitalized-word tokens that look like names (Latin or
    Cyrillic). Ignores:

      1. Sentence-initial words — natural capitalization in EN/RU,
         not entity claims. We split on sentence terminators and drop
         the first capitalized word of each segment.
      2. A common-starter ignore set — for queries / imperatives that
         start a question.

    This is intentionally lenient: audit's job is to catch FABRICATED
    entity NAMES (authors, books, words), not normal grammar."""
    # Common starters / function words to ignore even mid-sentence
    ignore = {
        # English
        "The", "This", "These", "Those", "Their", "Its", "It's",
        "What", "How", "When", "Where", "Why", "Which", "Who", "Whom",
        "Compare", "Show", "Find", "Tell", "Take", "Look", "Try",
        "Read", "Get", "Make", "Give", "See", "Ask", "Use",
        "Yes", "No", "Maybe", "Perhaps", "Also", "Otherwise",
        # Russian
        "Что", "Если", "Как", "Какой", "Какие", "Какая", "Какое",
        "Можно", "Можете", "Например", "Какова",
        "Сравни", "Покажи", "Найди", "Топ", "Дай", "Сколько", "Кто",
        "Расскажи", "Уточни", "Скажи", "Посмотри", "Попробуй",
        "Вот", "Это", "Так", "Тут", "Там", "Тогда", "Поэтому",
        "Можешь", "Хочешь", "Будет", "Был", "Была", "Было", "Были",
    }
    out: set[str] = set()
    # Split on sentence terminators + line breaks
    for segment in re.split(r"[.!?\n]+", text):
        if not segment.strip():
            continue
        words = re.findall(r"\b[A-ZА-ЯЁ][A-Za-zА-Яа-яё'\-]{2,}", segment)
        if not words:
            continue
        # Drop the first capitalized word — sentence-initial
        # capitalization is not an entity claim.
        for w in words[1:]:
            if w not in ignore:
                out.add(w)
    return out


def _payload_text_pool(view: RenderableView, skeleton: str) -> str:
    """Single text blob containing everything the prose is allowed to
    reference: the rendered skeleton plus a serialized view payload.
    Lower-cased for case-insensitive containment checks."""
    payload_json = ""
    try:
        payload_json = json.dumps(view.payload, ensure_ascii=False,
                                    default=str)
    except Exception:
        payload_json = str(view.payload)
    pool = (skeleton or "") + "\n" + payload_json
    return pool.lower()


def verify_prose(*, prose: str, view: RenderableView,
                  skeleton: str) -> list[str]:
    """Return list of verification failures (empty = passed).

    Two checks:
      1. Every number in prose appears in skeleton or payload.
      2. Every proper noun in prose appears in skeleton or payload.

    Both are LENIENT — the pool includes the skeleton (which already
    contains everything from payload that the deterministic template
    chose to render). Anything in pool is "allowed".
    """
    failures: list[str] = []
    if not prose or not prose.strip():
        return failures      # empty prose is fine — caller will drop anyway

    pool = _payload_text_pool(view, skeleton)
    pool_numbers = _extract_numbers_normalized(pool)
    pool_lc = pool

    # Number audit
    prose_numbers = _extract_numbers_normalized(prose)
    for n in prose_numbers:
        if n not in pool_numbers:
            # Tolerate small numbers (rank labels: «top-3», «1-3», «2-х»).
            # If the number is ≤ small_threshold AND it's a single
            # digit / two digits, allow it as a natural-language tally.
            if n.isdigit() and len(n) <= 2 and int(n) <= 20:
                continue
            failures.append(f"prose number not in payload: {n!r}")

    # Proper noun audit
    prose_names = _extract_proper_nouns(prose)
    for name in prose_names:
        if name.lower() not in pool_lc:
            failures.append(f"prose entity not in payload: {name!r}")

    return failures


# =====================================================================
# Bind entry point
# =====================================================================

V5_PROSE_BINDER_ENABLED = os.environ.get("WC_V5_PROSE", "off") == "on"

DEFAULT_LLM_TIMEOUT_S = 8.0
DEFAULT_LLM_MODEL = os.environ.get("WC_LLM_MODEL", "qwen3:14b")
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")


def _default_llm_call(system_prompt: str, user_prompt: str,
                       /, *, timeout_s: float = 8.0) -> str:
    """Default LLM call via Ollama HTTP. Production path."""
    try:
        import requests
    except ImportError:
        return ""
    url = f"{DEFAULT_OLLAMA_HOST.rstrip('/')}/api/chat"
    payload = {
        "model": DEFAULT_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_ctx": 4096},
    }
    try:
        r = requests.post(url, json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        return (data.get("message") or {}).get("content", "") or ""
    except Exception as e:
        log.warning("default LLM call failed: %s", e)
        return ""


def _parse_json_strict(raw: str) -> dict:
    """Parse a JSON object. Tolerates leading/trailing markdown fences."""
    s = (raw or "").strip()
    if s.startswith("```"):
        # Strip ```json …``` or ``` …```
        s = s.lstrip("`").lstrip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
        if s.endswith("```"):
            s = s[:-3].rstrip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Tolerate {"...": "..."} at the start with garbage after
        m = re.search(r"\{[^{}]*\}", s)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def bind_prose(
    *,
    view: RenderableView,
    skeleton: str,
    question: str,
    history: list | None = None,
    llm_call: LLMCallable | None = None,
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S,
    language: Literal["ru", "en"] | None = None,
    enable_llm: bool = True,
) -> ProseResult:
    """Bind prose intro + next-steps to a deterministic view.

    Args:
      view             — the RenderableView already rendered to skeleton
      skeleton         — output of template_executor.render_view(view)
      question         — user's original query
      history          — prior conversation turns (unused in v5.0; kept
                         for forward-compat with conversational binding)
      llm_call         — injectable LLM callable; default = Ollama HTTP
      llm_timeout_s    — hard cap on LLM call wall-clock
      language         — 'ru' / 'en'; default = inferred from question
      enable_llm       — set False to skip Phase B entirely (returns
                         empty ProseResult). Used in tests.

    Returns:
      ProseResult — intro/next_steps populated only if verification
                    passes; verification_failures lists reasons on drop.
    """
    result = ProseResult()
    if not enable_llm:
        return result

    lang = language or _detect_language(question)
    system_prompt = (_SYSTEM_PROMPT_RU if lang == "ru" else _SYSTEM_PROMPT_EN)
    user_prompt = _build_user_prompt(
        question=question,
        skeleton=skeleton,
        payload_summary=_compact_payload(view),
        language=lang,
    )

    llm = llm_call or _default_llm_call
    t0 = time.perf_counter()
    raw = ""
    try:
        raw = llm(system_prompt, user_prompt, timeout_s=llm_timeout_s)
    except Exception as e:
        log.warning("ProseBinder LLM call raised: %s", e)
        result.verification_failures.append(f"llm_exception: {e}")
        return result
    result.llm_elapsed_s = time.perf_counter() - t0
    result.used_llm = True

    if not raw or not raw.strip():
        result.verification_failures.append("llm_returned_empty")
        return result

    parsed = _parse_json_strict(raw)
    if not isinstance(parsed, dict):
        result.verification_failures.append("llm_returned_non_dict")
        return result

    intro_raw = (parsed.get("intro") or "").strip()
    steps_raw = parsed.get("next_steps") or []
    if not isinstance(steps_raw, list):
        steps_raw = [str(steps_raw)]
    steps_raw = [str(s).strip() for s in steps_raw if str(s).strip()]

    # Hard length cap on intro
    if len(intro_raw) > 350:
        intro_raw = intro_raw[:350].rstrip() + "…"

    # Audit: prose must not reference anything outside payload+skeleton
    combined_prose = intro_raw + "\n" + "\n".join(steps_raw)
    failures = verify_prose(prose=combined_prose, view=view,
                             skeleton=skeleton)
    if failures:
        result.verification_passed = False
        result.verification_failures = failures
        # Drop prose entirely — fail-safe
        return result

    # Audit passed — populate
    result.intro = intro_raw or None
    result.next_steps = steps_raw[:3]
    return result


# =====================================================================
# Helpers
# =====================================================================

def _detect_language(text: str) -> Literal["ru", "en"]:
    """Best-effort language detection. Cyrillic char in text → ru."""
    if not text:
        return "ru"
    cyr = sum(1 for ch in text if "А" <= ch <= "я" or ch in "ёЁ")
    lat = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return "ru" if cyr >= lat else "en"


# Module-level marker
V5_PROSE_BINDER_VERSION = "0.1"


__all__ = [
    "LLMCallable", "ProseResult", "bind_prose",
    "verify_prose",
    "V5_PROSE_BINDER_ENABLED", "V5_PROSE_BINDER_VERSION",
]
