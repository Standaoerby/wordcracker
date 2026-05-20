"""v4 LLM Planner — emits a PlanSpec JSON DAG for compound / living-language queries.

Architecture
============

The rules-based pipeline (`intent.py` → `entities.py` → `plan.py`) covers
the well-trodden ~70% of queries in ≤50 ms. When it falls to clarify (or
when entities are rich but no plan template fits — the symptom we treat
in `_smart_clarify_recipe`), this module steps in.

Contract:
    1. ONE inference call to qwen3:14b with `format=json` and a strict
       system prompt assembled from the tool catalog + few-shot examples.
    2. Output is parsed into a `PlanSpec` and validated against the
       registry. Invalid → retry once with a stricter "your previous
       output was invalid because…" suffix. Still invalid → return
       clarify with the original user query as scaffold.
    3. NEVER picks tools in a loop. The plan is emitted once and the
       router executes it deterministically.

This is **not** v1's agentic loop — there's no iteration, no
think-then-act-then-think drift. The LLM produces a plan; we execute.
Hallucinated tools bounce at validation. Hallucinated args bounce at
the per-step coerce/dispatch boundary. Numeric / factual errors get
caught downstream by the existing critic.

Observability
=============
Every plan attempt is logged via `obs.log_request` shape with extra
fields:
    via=v4_llm_planner
    plan_attempts=1|2
    plan_valid=True|False
    plan_steps=N

The PlanSpec itself is included so Stan can review what the LLM
generated for each query in the JSONL log.

Feature flag
============
`WC_LLM_PLANNER=on` enables this module from `rag_v2.py`. Off by default
during alpha rollout. When OFF, the existing clarify path is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

import requests

from scripts.v2.planner import plan_spec
from scripts.v2.planner.plan_spec import (
    PlanSpec,
    ValidationReport,
    from_json as plan_from_json,
    validate as validate_plan,
)
from scripts.v2.planner.tool_catalog import build_planner_prompt

log = logging.getLogger("wordcracker.v2.planner.llm_planner")


# ---------- configuration ----------


LLM_PLANNER_ENABLED = os.environ.get("WC_LLM_PLANNER", "").lower() in {
    "1", "on", "true", "yes",
}
LLM_PLANNER_MODEL = os.environ.get(
    "WC_LLM_PLANNER_MODEL",
    os.environ.get("WC_LLM_MODEL", "qwen3:14b"),
)
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
LLM_PLANNER_TIMEOUT = float(os.environ.get("WC_LLM_PLANNER_TIMEOUT_S", "30"))
# qwen3:14b plan tokens — most plans fit in 600 tokens; allow 1200 for
# complex compound queries with rationales.
LLM_PLANNER_NUM_PREDICT = int(os.environ.get("WC_LLM_PLANNER_NUM_PREDICT", "1200"))


# ---------- cache ----------


_CACHE: "OrderedDict[str, PlanSpec]" = OrderedDict()
_CACHE_MAX = 128
_LOCK = threading.Lock()


def _cache_key(text: str, history_tail: str = "") -> str:
    return (text.strip().lower()[:300] + "||" + history_tail.strip().lower()[:200])


# ---------- system prompt ----------


_SYSTEM_PROMPT_CACHE: Optional[str] = None


def _system_prompt() -> str:
    """Build the system prompt once per process. Cached because
    `build_planner_prompt` walks the entire tool registry + renders
    few-shot examples — not free."""
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = build_planner_prompt()
    return _SYSTEM_PROMPT_CACHE


def reset_prompt_cache_for_tests() -> None:
    """Force the system prompt to be re-built on next call. Used by
    tests that mutate the registry."""
    global _SYSTEM_PROMPT_CACHE
    _SYSTEM_PROMPT_CACHE = None


def reset_cache_for_tests() -> None:
    with _LOCK:
        _CACHE.clear()


# ---------- main entry point ----------


class PlannerResult:
    """Lightweight return type. Either a valid PlanSpec or a clarify
    string (the planner's last word — we don't fall back further)."""

    __slots__ = ("plan", "clarify", "attempts", "elapsed_s", "validation",
                 "raw_text")

    def __init__(self, *, plan: Optional[PlanSpec] = None,
                 clarify: Optional[str] = None,
                 attempts: int = 0, elapsed_s: float = 0.0,
                 validation: Optional[ValidationReport] = None,
                 raw_text: str = ""):
        self.plan = plan
        self.clarify = clarify
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.validation = validation
        self.raw_text = raw_text

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.plan.clarify is None

    def __repr__(self) -> str:
        return (f"PlannerResult(ok={self.ok}, attempts={self.attempts}, "
                f"elapsed={self.elapsed_s:.2f}s)")


def plan_query(text: str, *,
                history: Optional[list[dict]] = None,
                max_attempts: int = 2,
                ) -> Optional[PlannerResult]:
    """Ask the LLM to produce a PlanSpec for `text`.

    Returns None if the planner is disabled (caller falls back to existing
    clarify). Returns PlannerResult with `ok=False` and a `clarify` string
    when the LLM declines or all retries fail validation.

    `history` is the chat conversation up to but not including this turn.
    We only thread the most-recent user message into the prompt so the
    context stays bounded; multi-turn entity backfill is the job of
    `history.merge_with_history`, not the planner.
    """
    if not LLM_PLANNER_ENABLED:
        return None
    if not text or not text.strip():
        return None

    # Sprint 20+ — for follow-up queries the LLM needs to see BOTH the
    # prior user message (intent context) AND the prior assistant
    # response (the actual data the user is referring to with «эти
    # слова» / «их» / «убери из них имена собственные»). Without the
    # assistant body, the LLM can't know what to translate / filter /
    # transform. Stan chose this routing on 2026-05-19.
    prior_user = ""
    prior_assistant = ""
    if history:
        for msg in reversed(history):
            role = msg.get("role") if isinstance(msg, dict) else None
            if role == "assistant" and not prior_assistant:
                content = (msg.get("content") or "").strip()
                if content:
                    prior_assistant = _summarize_assistant_for_planner(content)
            if role == "user" and not prior_user:
                cand = (msg.get("content") or "").strip()
                if cand and cand != text.strip():
                    prior_user = cand
            if prior_user and prior_assistant:
                break

    # Cache key includes a fingerprint of the prior turn so two different
    # conversations with the same `text` don't collide.
    history_fp = (prior_user[:120] + "|" + prior_assistant[:120]).lower()
    key = _cache_key(text, history_fp)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return PlannerResult(plan=cached, attempts=0, elapsed_s=0.0)

    user_msg = text.strip()
    if prior_user or prior_assistant:
        ctx_parts: list[str] = []
        if prior_user:
            ctx_parts.append(f"Previous user message: {prior_user[:300]}")
        if prior_assistant:
            ctx_parts.append(
                f"Previous assistant response (truncated):\n"
                f"{prior_assistant[:1800]}"
            )
        user_msg = "\n\n".join(ctx_parts) + f"\n\nCurrent query: {user_msg}"

    t_overall = time.perf_counter()
    last_validation: Optional[ValidationReport] = None
    last_raw_text = ""
    retry_hint = ""

    for attempt in range(1, max_attempts + 1):
        raw_text = _call_ollama(user_msg + retry_hint)
        last_raw_text = raw_text
        if not raw_text:
            log.warning("llm_planner attempt %d: empty/failed Ollama response",
                        attempt)
            retry_hint = ("\n\n[retry] Your previous response was empty. "
                          "Emit a JSON plan or `{\"clarify\":\"...\"}`.")
            continue

        parsed = _parse_json(raw_text)
        if parsed is None:
            log.warning("llm_planner attempt %d: could not parse JSON: %.200s",
                        attempt, raw_text)
            retry_hint = ("\n\n[retry] Your previous response was not valid "
                          "JSON. Output strict JSON only, no markdown fences "
                          "or prose.")
            continue

        try:
            plan = plan_from_json(parsed)
        except (ValueError, TypeError) as e:
            log.warning("llm_planner attempt %d: PlanSpec build failed: %s",
                        attempt, e)
            retry_hint = (f"\n\n[retry] PlanSpec build failed: {e}. "
                          "Fix the schema and try again.")
            continue

        # Clarify-only plans are terminal and valid.
        if plan.clarify and not plan.steps:
            with _LOCK:
                _CACHE[key] = plan
                if len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
            return PlannerResult(
                plan=plan, clarify=plan.clarify,
                attempts=attempt,
                elapsed_s=time.perf_counter() - t_overall,
                raw_text=raw_text,
            )

        report = validate_plan(plan)
        last_validation = report
        if report.ok:
            with _LOCK:
                _CACHE[key] = plan
                if len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
            return PlannerResult(
                plan=plan, attempts=attempt,
                elapsed_s=time.perf_counter() - t_overall,
                validation=report, raw_text=raw_text,
            )
        # Validation failed — build a retry hint summarizing the errors
        # AND including the offending tools' actual input_schema so the
        # LLM can correct itself precisely.
        err_summary = "; ".join(
            f"{i.code} ({i.message})" for i in report.errors()[:5]
        )
        schema_hints = _schema_hints_for_failed_tools(plan, report)
        log.warning("llm_planner attempt %d invalid: %s", attempt, err_summary)
        retry_hint = (
            f"\n\n[retry] Your previous plan failed validation:\n"
            f"  errors: {err_summary}\n"
        )
        if schema_hints:
            retry_hint += (
                f"  schemas of failing tools:\n{schema_hints}\n"
                f"Use ONLY args from input_schema.properties. "
                f"Do NOT invent fields not listed there."
            )
        # Sprint 20+ — Stan prod showed qwen3:14b corrupting arg names
        # with non-ASCII characters mid-token (target_lang → target身).
        # Explicit ASCII reminder when the error mentions non-ASCII chars.
        if any(ord(ch) > 127 for ch in err_summary):
            retry_hint += (
                "\n  ⚠ The previous output contained NON-ASCII characters "
                "inside argument names. JSON keys and argument names MUST "
                "be exact ASCII as listed in the schema. Copy the names "
                "character-for-character — do not translate them."
            )
        # If validator rejected too_many_steps, also remind about the
        # 10-step fan-out cap.
        if any("too_many_steps" in i.code for i in report.errors()):
            retry_hint += (
                "\n  ⚠ Cap parallel fan-out at 10 steps. If user asked "
                "for «all»/«100», emit 10 + mention cap in rationale; "
                "do not try to cover all in one plan."
            )

    # All retries failed → graceful clarify with original entities scaffold.
    elapsed = time.perf_counter() - t_overall
    log.warning("llm_planner exhausted retries for %r (%.2fs)",
                text[:80], elapsed)
    return PlannerResult(
        clarify=(
            "Не получилось разобрать запрос автоматически. "
            "Попробуй переформулировать: упомяни автора / книгу / "
            "конкретную метрику явно — например «фирменные слова Wodehouse» "
            "или «уровень сложности Pride and Prejudice»."
        ),
        attempts=max_attempts,
        elapsed_s=elapsed,
        validation=last_validation,
        raw_text=last_raw_text,
    )


# ---------- Ollama call ----------


def _call_ollama(user_msg: str) -> str:
    """Single Ollama chat call with format=json. Returns content text
    (may or may not be valid JSON; parser decides)."""
    # Sprint 22+ alpha5 — Token Budget. v4 planner prompt is mostly
    # the system message (tool catalog ~13KB + few-shot examples
    # ~5KB) which is fixed. The variable part is user_msg (history
    # context + current query, already pruned at call site). We pass
    # explicit num_ctx so Ollama allocates a sufficient window;
    # otherwise it falls back to model default (8k for qwen3:14b)
    # which barely fits the system prompt.
    from scripts.v2.token_budget import TokenBudget
    budget = TokenBudget(model=LLM_PLANNER_MODEL)
    payload = {
        "model": LLM_PLANNER_MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.0,
            "num_predict": LLM_PLANNER_NUM_PREDICT,
            "num_ctx": budget.ctx,
        },
        "keep_alive": -1,
    }
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/chat",
                           json=payload,
                           timeout=LLM_PLANNER_TIMEOUT)
        r.raise_for_status()
        resp = r.json()
        return ((resp.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        log.warning("llm_planner Ollama call failed: %s", e)
        return ""


# ---------- JSON extraction ----------


_FENCE_RE = None  # compiled lazily


def _parse_json(text: str) -> Optional[dict]:
    """Pull a JSON object out of the LLM response.

    Tolerates accidental markdown fences (`` ```json … ``` `` ) and
    leading prose like "Here is the plan: { ... }". Stops at the first
    top-level object. Returns None on hopeless garbage.
    """
    if not text:
        return None
    s = text.strip()
    # strip fences if present
    if s.startswith("```"):
        # remove opening fence up to newline
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
        # remove trailing fence
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    # Direct parse first — `format=json` should give us a clean object.
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Fallback: find the first { and try to balance braces.
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(s[start:], start=start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = s[start:i + 1]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
    return None


def _summarize_assistant_for_planner(content: str, max_chars: int = 1800) -> str:
    """Compact a prior assistant response for the v4 planner's prompt.

    The renderer often produces multi-paragraph Markdown with a table.
    The planner mainly needs:
      - the table's column 1 (for word-list follow-ups)
      - the first paragraph (intent context)
      - some sample row data

    Truncating naively at `max_chars` cuts mid-table and confuses the
    LLM. Better: keep first 600 chars (header + intro), then the first
    ~50 table rows column-1, then the closing paragraph if any. Skip
    huge data columns the LLM doesn't need.
    """
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    lines = content.splitlines()
    # Find first markdown table row
    table_start = None
    for i, line in enumerate(lines):
        if "|" in line and line.count("|") >= 2:
            table_start = i
            break
    if table_start is None:
        # No table — just take the head
        return content[:max_chars].rstrip() + "\n…[truncated]"
    head = "\n".join(lines[:table_start]).strip()
    # Compress table to column 1 only, max 50 rows
    table_rows: list[str] = []
    for line in lines[table_start:table_start + 60]:
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        # Drop leading/trailing empty cells from `| x | y |` split
        parts = [p for p in parts if p]
        if not parts:
            continue
        # Separator rows like `---|---|---`
        if all(set(p) <= set("-: ") for p in parts):
            continue
        first = parts[0]
        table_rows.append(f"| {first} |")
    body = "\n".join(table_rows) if table_rows else ""
    summary = f"{head[:600]}\n\n{body}".strip()
    if len(summary) > max_chars:
        summary = summary[:max_chars].rstrip() + "\n…[truncated]"
    return summary


def _schema_hints_for_failed_tools(plan, report) -> str:
    """For each step that failed validation, dump its tool's
    input_schema.properties so the retry prompt tells the LLM exactly
    what's allowed. This was the missing piece on Stan 2026-05-19
    prod — LLM emitted `basis=pub_year` for word_freq_timeline because
    it never saw the schema constraint."""
    try:
        from scripts.v2.tool_registry import REGISTRY
    except ImportError:
        return ""
    failed_step_ids = {i.step_id for i in report.errors() if i.step_id}
    if not failed_step_ids:
        return ""
    tools_seen: set[str] = set()
    lines: list[str] = []
    for step in plan.steps:
        if step.id not in failed_step_ids:
            continue
        if step.tool in tools_seen:
            continue
        tools_seen.add(step.tool)
        spec = REGISTRY.get(step.tool)
        if not spec:
            continue
        schema = getattr(spec, "input_schema", {}) or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        prop_summary = ", ".join(
            f"{k}{'*' if k in required else ''}: {(v or {}).get('type', 'any')}"
            for k, v in props.items()
        )
        lines.append(f"    {step.tool}: {{ {prop_summary} }}  (* = required)")
    return "\n".join(lines)


__all__ = [
    "LLM_PLANNER_ENABLED",
    "PlannerResult",
    "plan_query",
    "reset_cache_for_tests",
    "reset_prompt_cache_for_tests",
]
