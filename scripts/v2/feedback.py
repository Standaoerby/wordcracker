"""User feedback collection — «неправильный ответ» button storage.

Sprint 22+: Stan asked for a way to flag bad answers in-flight so
fixes can be prioritized from real user reports instead of waiting
for external Claude rounds.

Storage: append-only JSONL at /workspace/spgc/derived/v2_feedback/
bad-YYYY-MM-DD.jsonl. Same bind-mount as v2_logs so admin/ops can
grep / tail it directly on the host.

Each entry:
  {ts, ip, question, answer, intent, intent_confidence, tool_calls,
   elapsed_sec, render_meta_snippet, critic_summary, user_note}

The full ToolResult.data is NOT included — it can be huge. Instead
we keep a tool-call summary (name + args + ok), and the user can
re-run the exact question (it's persisted) to reproduce the full
tool trace when they're debugging.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("wordcracker.v2.feedback")

FEEDBACK_DIR = Path(os.environ.get(
    "WC_V2_FEEDBACK_DIR",
    "/workspace/spgc/derived/v2_feedback",
))

_write_lock = threading.Lock()


def _today_path() -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return FEEDBACK_DIR / f"bad-{d}.jsonl"


def record_bad_answer(
    *,
    question: str,
    answer: str,
    intent: str | None = None,
    intent_confidence: float | None = None,
    tool_calls: list[dict] | None = None,
    elapsed_sec: float | None = None,
    render_meta: dict | None = None,
    critic_summary: str | None = None,
    user_note: str | None = None,
    history: list[dict] | None = None,
    ip: str | None = None,
) -> dict:
    """Append a bad-answer record to today's JSONL. Returns the saved
    record (with `id`, `ts`) for client confirmation.

    Inputs are best-effort — only `question` and `answer` are mandatory.
    Caller (chat_server) gathers what's available from the assistant
    turn payload and the last v2 response meta.
    """
    if not question or not isinstance(question, str):
        raise ValueError("question must be a non-empty string")
    if not answer or not isinstance(answer, str):
        raise ValueError("answer must be a non-empty string")

    record_id = uuid.uuid4().hex[:12]
    ts = datetime.now(timezone.utc).isoformat()

    # Cap large fields — admin will copy-paste these from the dashboard,
    # don't make individual records huge. Caller's responsibility to
    # send only what's useful; we defense-in-depth here too.
    rec = {
        "id": record_id,
        "ts": ts,
        "ip": (ip or "")[:80],
        "question": question[:5000],
        "answer": answer[:20000],
        "intent": intent,
        "intent_confidence": intent_confidence,
        "elapsed_sec": elapsed_sec,
        "tool_calls": [
            {
                "name": (tc.get("name") or "")[:80],
                "args": tc.get("args"),
                "ok": tc.get("ok"),
                "runtime_ms": tc.get("runtime_ms"),
            }
            for tc in (tool_calls or [])[:20]
        ],
        "render_meta": {
            k: render_meta.get(k)
            for k in ("prompt_tokens", "eval_tokens",
                       "budget_utilization_pct", "confabulation_risk",
                       "shrink_applied", "shrink_actions",
                       "budget_fits")
            if render_meta and k in render_meta
        } if render_meta else None,
        "critic_summary": (critic_summary or "")[:500] if critic_summary else None,
        "user_note": (user_note or "")[:1000] if user_note else None,
        "history_turns": len(history or []),
    }

    path = _today_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        log.error("feedback append failed: %s (%s)", e, path)
        raise

    log.info("flag_bad_answer recorded: id=%s intent=%s note=%r",
             record_id, intent, (user_note or "")[:80])
    return rec


def list_recent(*, days_back: int = 7, limit: int = 200) -> list[dict]:
    """Read recent JSONL records for admin display. Newest first."""
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for delta in range(days_back):
        day = today.fromordinal(today.toordinal() - delta).isoformat()
        p = FEEDBACK_DIR / f"bad-{day}.jsonl"
        if not p.exists():
            continue
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    out.append(rec)
        except OSError as e:
            log.warning("feedback read failed for %s: %s", p, e)
            continue
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out[:limit]


__all__ = ["record_bad_answer", "list_recent", "FEEDBACK_DIR"]
