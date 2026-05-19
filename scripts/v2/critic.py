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


_CRITIC_PROMPT = """Ты — критик-верификатор. Получи ответ + tool_results на котором он построен. Твоя задача — найти ТОЛЬКО ОТКРОВЕННО ВЫДУМАННЫЕ утверждения. ОЧЕНЬ ВАЖНО: ложные срабатывания дороже пропусков. Если не уверен — НЕ флагай.

ДЕФОЛТ: verified=true. Флагай ТОЛЬКО при чёткой фальсификации.

ПРИЗНАКИ ФАЛЬСИФИКАЦИИ (когда обязательно флагать):
- Число которого ВООБЩЕ нет ни в одном поле tool_results.data (типа «у Толстого 250 книг» когда tool_results показывают 47).
- PG id типа «PG12345» которого нет ни в одном tool_result.
- Книга названа канонически (e.g. "Anna Karenina") но её НЕТ в matches / data — а ответ говорит что есть.
- Цитата в кавычках которая не присутствует в samples / contexts / snippet любого tool.

НЕ ФЛАГАЙ (data echo — это нормально):
- Любую таблицу из tool_data — даже если ответ перечисляет 20 строк, считай таблицу одним agreement с data.
- Числа из data в перифразе («Holmes встречается 4045 раз» → ответ «4 тысячи раз» — это compression, не fabrication).
- Стилистические комментарии («это типично для викторианцев», «голос Wodehouse») если они общие.
- Заявления о coverage если в data есть coverage info.
- Названия инструментов и intent в ответе.
- Любые цифры внутри tool's `data` поля, даже если они в nested objects.

Верни ТОЛЬКО JSON без markdown, без объяснений:
{
  "verified": true|false,
  "unsupported_claims": [],   // максимум 2 строки; пустой массив = чисто
  "missing_caveats": [],
  "summary": "1 предложение"
}

Empty arrays = clean answer = verified true. Это наиболее частый случай. Не ищи проблем где их нет."""


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


# Sprint 11.1 + Sprint 17 extension: skip critic entirely for intents
# that are inherently table-data echoes — the renderer just copies rows
# from tool_results, the critic adds 3-5s of LLM latency without
# catching real hallucinations. Sprint 16 Phase D's programmatic numeric
# audit now catches the highest-value class of fabrications (made-up
# counts) deterministically, so the LLM critic's role on tabular intents
# is largely redundant.
_INTENT_SKIP_CRITIC = {
    # learning_words returns ranked rows; renderer literally copies them
    "learning",
    # top_authors_by / top_books_by_downloads: pure table
    "top_authors_books",
    # learning vocab passport already heavy-cached + audited
    "vocab_passport",
    # Sprint 17 extension — Phase E/F/G intents that are pure table echo:
    # author_lookup    → author_metadata.sample_titles (list of strings)
    # corpus_extremum  → top_authors_by(top=1) (1-row table)
    # book_extremum    → top_books_by_downloads(top=1) or clarify
    # topic_book_search → find_book_by_topic (dedupped book list)
    # book_pub_year    → find_book.matches[0].pub_year (single value)
    # book_lookup      → find_book.matches (title resolution table)
    # Numeric audit (Phase D) catches the fabricated-count failure mode
    # on these intents programmatically; LLM critic was just noise.
    "author_lookup",
    "corpus_extremum",
    "book_extremum",
    "topic_book_search",
    "book_pub_year",
    "book_lookup",
}


def review(answer: str, tool_results_summary: list[dict], *,
           intent: str = "unknown",
           model: str = CRITIC_MODEL,
           ollama_host: str | None = None) -> CriticVerdict:
    """Run the critic call. Returns trust() on any failure path.

    Skips the LLM round-trip entirely for table-data intents (Sprint 11.1
    add) — the critic over-flagged tabular answers in bench v2.0.7
    (23/32 noise vs ~3 real catches). For those intents the planner +
    tool envelope already gates correctness; the second LLM pass added
    cost without benefit.
    """
    if not CRITIC_ENABLED:
        return CriticVerdict.trust()
    if not answer or not answer.strip():
        return CriticVerdict.trust()
    if not tool_results_summary:
        # Pure no-tool answers (introduction, clarify, out_of_scope) — nothing
        # to verify against.
        return CriticVerdict.trust()
    if intent in _INTENT_SKIP_CRITIC:
        return CriticVerdict(
            verified=True, unsupported_claims=[], missing_caveats=[],
            summary=f"(critic skipped for table intent: {intent})",
        )

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

    unsupported = list(parsed.get("unsupported_claims") or [])
    caveats = list(parsed.get("missing_caveats") or [])
    # Sanity guard: when the critic flags >= MAX_FLAGS items, it's almost
    # certainly confused (the renderer just pulled rows from a table, every
    # number is "unsupported" from a strict perspective). Trust the answer
    # and surface only the count via summary so the UI badge stays useful.
    # Sprint 11.1: tightened to 2 (was 4) so even one borderline case
    # treats the critic as confused. Real hallucinations almost always
    # produce exactly 1 distinct flag (the one fabricated number); 3+ is
    # a critic-quality issue, not the renderer.
    MAX_FLAGS = 2
    if len(unsupported) > MAX_FLAGS:
        log.info("critic over-flagged %d claims for intent=%s — guarding to trust",
                 len(unsupported), payload_for_critic.get("intent"))
        return CriticVerdict(
            verified=True, unsupported_claims=[], missing_caveats=caveats,
            summary=(f"({len(unsupported)} weak flags suppressed — "
                     f"answer renders a table; treat numbers as data echo)"),
            raw_text=content[:500],
        )
    return CriticVerdict(
        verified=bool(parsed.get("verified", True)),
        unsupported_claims=unsupported,
        missing_caveats=caveats,
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
