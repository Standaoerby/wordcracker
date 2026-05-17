"""Critic pass — second-LLM verification of the rendered answer.

After the renderer emits its prose answer, we run a short low-temperature
critic call that sees:
  * the renderer's final text
  * the structured tool_results it should have been quoting from
  * the plan intent

Critic returns a JSON verdict:
  {
    "verified": bool,
    "unsupported_claims": [str, ...],   # short paraphrase of suspect claims
    "missing_caveats":   [str, ...],    # coverage / corpus gaps the answer
                                        # glossed over
    "summary":           str            # 1-sentence overall assessment
  }

The rag_v2 pipeline appends a "⚠️ Проверь" block to the answer when
unsupported_claims is non-empty. Numeric and named-entity claims are the
highest-value targets — those are what produced the v1.0e hallucination
bug ("PG1327 = Crime and Punishment").

Cost: ~3-5 s extra per query, gated on env WC_CRITIC=on (default on for v2).
Failures (network / JSON parse) fall through silently — the un-critiqued
answer still ships.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import requests

log = logging.getLogger("wordcracker.v2.critic")

CRITIC_MODEL = os.environ.get("WC_CRITIC_MODEL", os.environ.get(
    "WC_LLM_MODEL", "qwen3:14b"))
CRITIC_ENABLED = os.environ.get("WC_CRITIC", "on").lower() in ("on", "1", "true")
CRITIC_TIMEOUT_S = int(os.environ.get("WC_CRITIC_TIMEOUT", "30"))


_CRITIC_PROMPT = """Ты — критик-верификатор. Получи готовый ответ и данные tools, на которых он построен. Найди утверждения в ответе, которые НЕ подкреплены данными.

Особо ищи:
1. Числа (frequencies, counts, percentages, года) — должны быть в tool_results.
2. Имена PG ID — должны соответствовать tool_results.
3. Имена авторов / названия книг — должны быть в tool_results.
4. Цитаты — должны быть в tool_results (samples / contexts).
5. Гарантии coverage («у всех авторов», «во всём корпусе») — должны быть подкреплены coverage из tool_results.

Игнорируй:
- Общие лингвистические замечания без чисел.
- Структурные комментарии («это типично для викторианцев»).
- Стилистические перифразы tool data.

Верни ТОЛЬКО JSON без markdown:
{
  "verified": true_если_все_claims_подкреплены_else_false,
  "unsupported_claims": ["короткое описание неподкреплённого утверждения", ...],
  "missing_caveats": ["coverage warning который ответ проглотил", ...],
  "summary": "одно предложение общего вердикта"
}

Если ответ — extracted clarify / out_of_scope без данных — верни verified=true с пустыми списками."""


@dataclass
class CriticVerdict:
    verified: bool
    unsupported_claims: list[str]
    missing_caveats: list[str]
    summary: str
    raw_text: str = ""  # for debug

    @classmethod
    def trust(cls) -> "CriticVerdict":
        """Pass-through verdict — use when critic is disabled or skipped."""
        return cls(verified=True, unsupported_claims=[],
                   missing_caveats=[], summary="(critic disabled)")

    def has_issues(self) -> bool:
        return bool(self.unsupported_claims) or bool(self.missing_caveats)


def _build_payload_for_critic(answer: str, tool_results_summary: list[dict],
                              intent: str) -> dict:
    """Compact context window for the critic — keep it small so we don't
    burn 4k tokens just on prior tool dumps."""
    compact_results = []
    for r in tool_results_summary[:8]:
        compact_results.append({
            "tool": r.get("tool"),
            "ok": r.get("ok", True),
            "data": _shrink(r.get("data"), max_chars=600),
            "coverage": r.get("coverage"),
            "warnings": [w.get("message") for w in (r.get("warnings") or [])
                         if isinstance(w, dict)][:3],
        })
    return {
        "intent": intent,
        "tool_results": compact_results,
        "answer": answer[:3000],
    }


def _shrink(value, *, max_chars: int) -> object:
    """Return a json-safe summary that fits in max_chars."""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        s = str(value)
    if len(s) <= max_chars:
        return value
    return s[:max_chars] + "...(truncated)"


def review(answer: str, tool_results_summary: list[dict], *,
           intent: str = "unknown",
           model: str = CRITIC_MODEL,
           ollama_host: str | None = None) -> CriticVerdict:
    """Run the critic call. Returns trust() on any failure path."""
    if not CRITIC_ENABLED:
        return CriticVerdict.trust()
    if not answer or not answer.strip():
        return CriticVerdict.trust()
    if not tool_results_summary:
        # Pure no-tool answers (introduction, clarify, out_of_scope) — nothing
        # to verify against.
        return CriticVerdict.trust()

    host = ollama_host or os.environ.get("OLLAMA_HOST", "http://ollama:11434")
    payload_for_critic = _build_payload_for_critic(
        answer, tool_results_summary, intent)

    messages = [
        {"role": "system", "content": _CRITIC_PROMPT},
        {"role": "user", "content": "Контекст:\n```json\n"
                                    + json.dumps(payload_for_critic,
                                                 ensure_ascii=False, default=str)
                                    + "\n```"},
    ]
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.1},
        "think": False,
        "format": "json",
    }
    try:
        resp = requests.post(f"{host}/api/chat", json=body,
                             timeout=CRITIC_TIMEOUT_S)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        log.warning("critic call failed: %s", e)
        return CriticVerdict.trust()

    content = ((resp.json() or {}).get("message") or {}).get("content", "")
    if not content.strip():
        return CriticVerdict.trust()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        log.warning("critic JSON parse failed: %s; raw=%r", e, content[:200])
        return CriticVerdict.trust()

    return CriticVerdict(
        verified=bool(parsed.get("verified", True)),
        unsupported_claims=list(parsed.get("unsupported_claims") or []),
        missing_caveats=list(parsed.get("missing_caveats") or []),
        summary=str(parsed.get("summary") or "(no summary)"),
        raw_text=content[:500],
    )


def annotate_answer(answer: str, verdict: CriticVerdict) -> str:
    """Append a ⚠️ block to the answer if the critic flagged issues.

    Stays out of the way when verified — no decoration."""
    if verdict.verified and not verdict.has_issues():
        return answer
    lines = [answer.rstrip(), "", "---", ""]
    if verdict.unsupported_claims:
        lines.append("⚠️ **Проверь следующие утверждения** — они не подкреплены явными данными tool:")
        for c in verdict.unsupported_claims[:5]:
            lines.append(f"- {c}")
        lines.append("")
    if verdict.missing_caveats:
        lines.append("ℹ️ **Не упомянутые ограничения покрытия:**")
        for m in verdict.missing_caveats[:3]:
            lines.append(f"- {m}")
        lines.append("")
    lines.append(f"_Critic: {verdict.summary}_")
    return "\n".join(lines)
