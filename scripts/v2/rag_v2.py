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

⚠️ ПРАВИЛА:
1. **Не выдумывай числа, имена, цитаты.** Всё, что в ответе — должно быть в tool_results.
2. **Не предлагай вызвать ещё tools.** Tool calling уже завершён. Ты только рендеришь.
3. **Язык ответа = язык вопроса пользователя.**
4. **Markdown-таблицы для табличных данных.**
5. **В конце предложи 1-2 next-step вопроса** в формате «можно дальше спросить: "..." или "..."».
6. Если coverage низкое или есть warning'и — упомяни это.
7. **Если в payload есть поле `render_instructions` — это ПРИОРИТЕТНЫЕ правила, всегда им следуй.** Они описывают, как именно рендерить специфические данные, чтобы не перепутать индексы / годы / метрики.

Tool trace тебе передан как JSON. Каждая запись — {{tool, query, data, warnings, coverage}}. Возьми оттуда factual content."""


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


def _llm_render(question: str, plan: plan_mod.QueryPlan,
                results: list[ToolResult], *, model: str,
                ollama_host: str,
                history: list[dict] | None = None) -> str:
    """Send one /api/chat call with the render prompt + tool data. No tools."""
    render_instructions = _collect_render_instructions(results)
    summary_payload = {
        "intent": plan.intent,
        "explain": plan.explain,
        "conversation_context": _conversation_summary(history, plan),
        # Top-level priority instructions — the renderer was missing notes
        # buried inside per-tool `data`. Surface them explicitly.
        "render_instructions": render_instructions,
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
    return (resp.json().get("message", {}) or {}).get("content", "").strip()


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
    if intent.label == "clarify":
        try:
            from scripts.v2.planner import llm_intent
            llm_match = llm_intent.classify_with_llm(question, history)
            if llm_match is not None:
                intent = llm_match
        except Exception:
            pass
    plan = plan_mod.build(intent.label, entities)

    if plan.needs_clarify:
        return {
            "answer": plan.clarify_question or "Уточни запрос.",
            "tool_calls": [],
            "iterations": 0,
            "model": model,
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "intent": intent.label,
            "intent_confidence": intent.confidence,
        }
    if plan.out_of_scope_reason:
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

    rr = router_mod.execute(plan)

    if intent.label == "introduction":
        # Skip LLM — return the static intro the user expects.
        answer = _intro_text()
    else:
        try:
            answer = _llm_render(question, plan, rr.results,
                                 model=model, ollama_host=ollama_host,
                                 history=history)
        except Exception as e:
            log.exception("renderer LLM failed")
            answer = (f"[renderer error: {e}]\n\nRaw tool results:\n"
                      + "\n".join(json.dumps(r.to_dict(), ensure_ascii=False,
                                             default=str, indent=2)[:1000]
                                  for r in rr.results))

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
    intent = int_mod.classify(question)
    entities = ent_mod.extract(question)
    entities = history_mod.merge_with_history(entities, history, question)
    if intent.label == "clarify":
        inferred = history_mod.infer_followup_intent(question, history)
        if inferred:
            intent = int_mod.IntentMatch(label=inferred, confidence=0.75,
                                         matched_pattern="followup-inferred")
    # LLM fallback for free-form phrasings (Sprint 13).
    if intent.label == "clarify":
        try:
            from scripts.v2.planner import llm_intent
            llm_match = llm_intent.classify_with_llm(question, history)
            if llm_match is not None:
                intent = llm_match
        except Exception:
            pass
    plan = plan_mod.build(intent.label, entities)

    yield {"event": "intent", "label": intent.label,
           "confidence": intent.confidence, "explain": plan.explain}

    if plan.needs_clarify:
        yield {"event": "clarify", "question": plan.clarify_question or ""}
        yield {"event": "answer", "text": plan.clarify_question or ""}
        yield {"event": "done", "tool_calls": [], "iterations": 0,
               "elapsed_sec": round(time.perf_counter() - t0, 2)}
        return
    if plan.out_of_scope_reason:
        yield {"event": "out_of_scope", "reason": plan.out_of_scope_reason}
        yield {"event": "answer", "text": plan.out_of_scope_reason}
        yield {"event": "done", "tool_calls": [], "iterations": 0,
               "elapsed_sec": round(time.perf_counter() - t0, 2)}
        return

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
    if intent.label == "introduction":
        answer = _intro_text()
    else:
        try:
            answer = _llm_render(question, plan, results,
                                 model=model, ollama_host=ollama_host,
                                 history=history)
        except Exception as e:
            answer = f"[renderer error: {e}]"

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

    yield {"event": "critic",
           "verified": verdict.verified,
           "issues_flagged": verdict.has_issues(),
           "unsupported_claims_n": len(verdict.unsupported_claims),
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


_INTRO_PATH = Path(__file__).parent / "intro_text.md"


def _intro_text() -> str:
    if _INTRO_PATH.exists():
        return _INTRO_PATH.read_text(encoding="utf-8")
    return (
        f"Меня зовут {ASSISTANT_NAME}. Я аналитик корпуса Project Gutenberg "
        f"(~55 тыс. книг). Умею:\n\n"
        "**📊 Стилометрия:** фирменные слова автора (`affinity_by_author`), "
        "сравнение авторов, биграммы, лексическая разнообразность, "
        "Burrows Delta attribution и influences.\n\n"
        "**📚 Книги:** уровень сложности (Flesch+CEFR), архаизмы, "
        "эмоциональный профиль, фирменные слова книги.\n\n"
        "**🔤 Слова:** контексты, collocates, timeline по эпохам, polysemy, "
        "этимология через Wiktionary, emotion collocates.\n\n"
        "**🎓 Изучение:** vocab B1/B2/C1/rare, enrichment с переводом, "
        "Anki/Markdown/JSON export.\n\n"
        "**🌐 Корпус:** прогресс индексации, топ-авторы, топ-книги.\n\n"
        "**Пример сложного запроса:** «характерные прилагательные Оскара "
        "Уайльда в \"The Picture of Dorian Gray\"» или «слова латинского "
        "происхождения у Толкина-аналога — Уильяма Морриса в \"The Well "
        "at the World's End\"».\n\n"
        "Спрашивай как поставить вопрос правильно — подскажу."
    )
