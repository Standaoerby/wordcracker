"""S6 webapp — streaming agent loop for the wordcracker-api HTTP layer.

``stream_answer()`` adapts the v2 pipeline's ``ask_stream()`` events to the
public SSE contract (plan.md §4 / docs/webapp.md):

    thinking | token | tool_call | tool_result | table | trace | error | done

Yields ``{"event": str, "data": dict}``. Invariants:
  - the stream ALWAYS ends with a single ``done`` event (even after
    ``error``) whose envelope carries ``stop_reason``;
  - table cells are native scalars (N1 — see scripts/v2/table_extract.py);
  - ``data_only=True`` skips the renderer (and critic/audit) — tables +
    empty ``answer_md``; clarify/out-of-scope text still comes through;
  - client disconnect (``.close()`` on this generator) sets the
    cancel_event so the in-flight Ollama generation aborts within ~1 token.

chat_server (:8890) does NOT use this module — it keeps calling
``ask_stream()`` with default flags, byte-identical behaviour.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Iterator

from scripts.v2 import rag_v2
from scripts.v2.table_extract import extract_tables

log = logging.getLogger("wordcracker.api_loop")

# v2 events forwarded to the UI trace block as {"event":"trace","data":{kind,...}}
_TRACE_EVENTS = frozenset({
    "intent", "plan", "v4_plan", "critic", "clarify", "out_of_scope",
})


def _ev(event: str, data: dict) -> dict:
    return {"event": event, "data": data}


def stream_answer(question: str, data_only: bool = False,
                  **ask_kwargs) -> Iterator[dict]:
    """Run one query through the v2 pipeline, yielding contract events.

    ``ask_kwargs`` pass through to ``ask_stream`` (model, ollama_host —
    used by tests to point at a stub)."""
    query_id = str(uuid.uuid4())
    cancel_event = ask_kwargs.pop("cancel_event", None) or threading.Event()

    tables: list[dict] = []
    tool_trace: list[dict] = []
    answer_md = ""
    streamed_tokens = False
    saw_error = False
    last_tool_ok = True
    budget_exceeded = False
    done_emitted = False

    def _envelope(stop_reason: str) -> dict:
        return {
            "query_id": query_id,
            "answer_md": answer_md,
            "tables": tables,
            "entities": [],          # always [] until Sprint 10
            "tool_trace": tool_trace,
            "data_only": data_only,
            "stop_reason": stop_reason,
        }

    def _stop_reason() -> str:
        if budget_exceeded:
            return "max_iterations"
        if saw_error or not last_tool_ok:
            return "tool_error"
        return "complete"

    inner = rag_v2.ask_stream(
        question,
        cancel_event=cancel_event,
        stream_render=True,
        skip_render=data_only,
        **ask_kwargs,
    )
    try:
        for ev in inner:
            kind = ev.get("event")
            if kind == "start":
                continue
            if kind == "render_token":
                streamed_tokens = True
                answer_md += ev.get("delta") or ""
                yield _ev("token", {"delta": ev.get("delta") or ""})
            elif kind == "thinking":
                yield _ev("thinking", {"delta": ev.get("delta")
                                       or ev.get("text") or ""})
            elif kind == "tool_call":
                args = ev.get("args") or {}
                tool_trace.append({"name": ev.get("name"), "args": args,
                                   "elapsed": None, "ok": None})
                yield _ev("tool_call", {"name": ev.get("name"), "args": args})
            elif kind == "tool_result":
                name = ev.get("name")
                ok = bool(ev.get("ok"))
                elapsed = round((ev.get("ms") or 0) / 1000.0, 3)
                last_tool_ok = ok
                for entry in reversed(tool_trace):
                    if entry["name"] == name and entry["elapsed"] is None:
                        entry["elapsed"] = elapsed
                        entry["ok"] = ok
                        break
                yield _ev("tool_result",
                          {"name": name, "elapsed": elapsed, "ok": ok})
                tr = ev.get("_result")   # live ToolResult (stream_render=True)
                if tr is not None and ok:
                    for table in extract_tables(tr.tool, tr.data):
                        tables.append(table)
                        yield _ev("table", table)
            elif kind == "answer":
                text = ev.get("text") or ""
                answer_md = text  # authoritative (critic/audit annotations)
                if text and not streamed_tokens:
                    # Non-streamed short-circuits: repeat / clarify /
                    # out_of_scope / introduction — one full-text token.
                    yield _ev("token", {"delta": text})
            elif kind == "error":
                saw_error = True
                yield _ev("error", {"message": ev.get("message")
                                    or "internal error",
                                    "kind": ev.get("kind") or "pipeline"})
            elif kind == "done":
                done_emitted = True
                yield _ev("done", _envelope(_stop_reason()))
            elif kind == "budget_exceeded":
                budget_exceeded = True
                yield _ev("trace", {"kind": kind,
                                    **{k: v for k, v in ev.items()
                                       if k != "event"}})
            elif kind in _TRACE_EVENTS or kind:
                # Known trace events + forward-compat: anything new from
                # the v2 pipeline flows into the trace block; the client
                # ignores unknown kinds.
                yield _ev("trace", {"kind": kind,
                                    **{k: v for k, v in ev.items()
                                       if k != "event" and not k.startswith("_")}})
        if not done_emitted:
            # ask_stream returned without a done event (e.g. cancel mid-
            # pipeline). Keep the contract: the stream ends with done.
            yield _ev("done", _envelope(_stop_reason()))
    except GeneratorExit:
        # Client disconnected — abort the in-flight Ollama generation
        # (lands within ~1 token, see plan.md §3.4) and let the inner
        # generator unwind.
        cancel_event.set()
        inner.close()
        raise
    except Exception as e:
        log.exception("stream_answer: pipeline failed")
        saw_error = True
        yield _ev("error", {"message": f"{type(e).__name__}: {e}",
                            "kind": "pipeline"})
        yield _ev("done", _envelope("tool_error"))
