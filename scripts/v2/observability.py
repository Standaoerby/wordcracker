"""Structured request logging for v2.

Appends one JSONL line per request to /data/logs/v2/queries-YYYY-MM-DD.jsonl
with fields the status dashboard can aggregate:

  ts, request_id, question_truncated, intent, intent_confidence,
  plan_steps[], tool_calls[], cache_hits, total_elapsed_ms,
  critic_verified, critic_unsupported_n, answer_truncated

Best-effort: file open/write failures log a warning and continue. The
chat server never crashes on log issues.

A small in-memory ring buffer keeps the last 256 records for the
status_server card («v2 last 24h: intent histogram / slow tools / cache
hit rate»). The ring buffer also covers the case where /data/logs/v2 is
unwritable (smoke tests, dev box).

v5 Phase 0 extension ([[architecture_refactor_v5_plan]] §P9):
Adds `RequestTrace` — append-only object threaded through the pipeline so
every layer (intent / entity / plan / tool / render / critic / audit)
contributes its slot. Replaces the scatter-logging where each layer
writes ad-hoc fields into a flat dict. Phase 0: opt-in (gate via
WC_V5_TRACE env or per-call); Phase 6: replaces `log_request` entirely.

Coexists with `log_request` — `RequestTrace.finalize()` calls
`log_request` with a flat shape so the existing aggregator and status
dashboard keep working.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("wordcracker.v2.observability")

# Default log dir is inside the SPGC derived bind-mount so logs survive
# container restarts and are visible from the host without a separate
# /data/logs volume.
LOG_DIR = Path(os.environ.get(
    "WC_V2_LOG_DIR",
    "/workspace/spgc/derived/v2_logs",
))
RING_SIZE = int(os.environ.get("WC_V2_RING_SIZE", "256"))

_ring: deque[dict] = deque(maxlen=RING_SIZE)
_ring_lock = threading.Lock()


def _today_log_path() -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return LOG_DIR / f"queries-{d}.jsonl"


import sys as _sys


def log_llm_latency(tool: str, model: str, num_ctx, body: dict | None) -> None:
    """S-P2c (#1): emit ONE structured stderr line per ollama generate/chat
    call so per-tool LLM latency is greppable in container logs.

    `body` is the ollama response JSON (it carries `load_duration`,
    `eval_count`, `prompt_eval_count`, `total_duration` in nanoseconds /
    tokens). Replaces the coarse `_elapsed_s`-in-fixture-body signal (removed
    in #2) with a richer, deterministic-format line:

        [llm] tool=renderer model=wordcracker:v2 num_ctx=8192 load_ms=1234               eval_count=512 prompt_eval=2048 total_ms=8765

    Best-effort and exception-safe — a logging failure must NEVER break the
    actual LLM call (callers also wrap it, belt-and-suspenders).
    """
    try:
        b = body or {}

        def _ms(ns):
            return round(ns / 1e6) if isinstance(ns, (int, float)) else "?"

        def _v(x):
            return x if x is not None else "?"

        print(
            f"[llm] tool={tool} model={model} num_ctx={_v(num_ctx)} "
            f"load_ms={_ms(b.get('load_duration'))} "
            f"eval_count={_v(b.get('eval_count'))} "
            f"prompt_eval={_v(b.get('prompt_eval_count'))} "
            f"total_ms={_ms(b.get('total_duration'))}",
            file=_sys.stderr, flush=True,
        )
    except Exception:
        # observability must never take down the request path
        pass


def log_request(payload: dict) -> None:
    """Stash a structured record. Caller passes a dict already shaped for
    aggregation; we add timestamp + request_id if missing."""
    record = dict(payload)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    record.setdefault("request_id", uuid.uuid4().hex[:12])

    with _ring_lock:
        _ring.append(record)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_today_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        log.warning("log_request failed: %s", e)


def recent_records() -> list[dict]:
    with _ring_lock:
        return list(_ring)


def aggregate_recent(window_n: int | None = None) -> dict:
    """Roll-up the ring buffer into a status-dashboard summary."""
    with _ring_lock:
        records = list(_ring)
    if window_n is not None:
        records = records[-window_n:]
    if not records:
        return {"total": 0}

    intents: Counter[str] = Counter(r.get("intent", "?") for r in records)
    tool_times: dict[str, list[int]] = {}
    cache_hits = 0
    cache_total = 0
    critic_flagged = 0
    elapsed_total = 0
    for r in records:
        elapsed_total += r.get("total_elapsed_ms", 0)
        for tc in r.get("tool_calls") or []:
            n = tc.get("name", "?")
            tool_times.setdefault(n, []).append(tc.get("runtime_ms", 0))
            cache_total += 1
            if tc.get("cache_hit"):
                cache_hits += 1
        if r.get("critic_unsupported_n", 0) > 0:
            critic_flagged += 1

    slow_tools = []
    for name, times in tool_times.items():
        if not times:
            continue
        avg = sum(times) // len(times)
        p95 = sorted(times)[max(0, int(len(times) * 0.95) - 1)]
        slow_tools.append({"tool": name, "n": len(times),
                           "avg_ms": avg, "p95_ms": p95})
    slow_tools.sort(key=lambda x: x["p95_ms"], reverse=True)

    return {
        "total": len(records),
        "intents": dict(intents.most_common()),
        "slow_tools": slow_tools[:10],
        "cache_hit_rate": (cache_hits / cache_total) if cache_total else 0.0,
        "cache_hits": cache_hits,
        "cache_calls": cache_total,
        "critic_flagged": critic_flagged,
        "avg_elapsed_ms": elapsed_total // len(records) if records else 0,
    }


def recent_failures(limit: int = 50) -> list[dict]:
    """Return the most recent N records where is_failure=True.

    v2.7 admin endpoint: Stan wants a queryable «what users asked but
    didn't get an answer for» view. Reads from the in-process ring
    buffer (last 256 by default) — newer records first. JSONL on disk
    still has the full history; this helper is for the live admin UI."""
    with _ring_lock:
        records = list(_ring)
    fails = [r for r in records if r.get("is_failure")]
    return list(reversed(fails))[:limit]


def _read_jsonl_failures(days_back: int = 2,
                          hard_limit: int = 2000) -> list[dict]:
    """Read failure records from on-disk JSONL logs.

    v2.10.1: admin_server runs in a separate Python process from
    chat_server even though both live inside gutenberg-lab. The
    in-memory `_ring` only sees events that hit ITS process. The
    JSONL log on disk is shared — admin can read it to surface
    failures recorded by chat.

    Walks the most recent `days_back` JSONL files (today + yesterday by
    default), parses each line, keeps only `is_failure=True`. Returns
    newest-first, capped at `hard_limit` to bound memory."""
    files: list[Path] = []
    if not LOG_DIR.exists():
        return []
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    for d in range(days_back):
        day = (now - timedelta(days=d)).strftime("%Y-%m-%d")
        p = LOG_DIR / f"queries-{day}.jsonl"
        if p.exists():
            files.append(p)
    rows: list[dict] = []
    for p in files:
        try:
            # Read whole file then iterate — these files are append-only
            # and small (one line per chat request).
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("is_failure"):
                        rows.append(r)
        except OSError as e:
            log.warning("read_jsonl_failures failed for %s: %s", p, e)
    # Newest first by ts (ISO strings sort correctly)
    rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return rows[:hard_limit]


def recent_failures_combined(limit: int = 50) -> list[dict]:
    """Union of in-memory ring buffer + on-disk JSONL. For admin /failed
    page that reads from a separate process and would otherwise miss the
    chat process's ring buffer."""
    ring = recent_failures(limit=limit)
    disk = _read_jsonl_failures(days_back=2, hard_limit=limit * 4)
    seen = set()
    out: list[dict] = []
    for r in ring + disk:
        rid = r.get("request_id") or (r.get("ts", "") + r.get("question_truncated", ""))
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def top_failed_phrases_combined(top_n: int = 10) -> list[dict]:
    """Same as top_failed_phrases but reads from both in-memory ring
    AND on-disk JSONL. For admin server which runs as a separate
    process and would otherwise see an empty ring."""
    return _aggregate_phrases(
        recent_failures(limit=10_000)
        + _read_jsonl_failures(days_back=3, hard_limit=10_000),
        top_n=top_n,
    )


def _aggregate_phrases(fails: list[dict], top_n: int = 10) -> list[dict]:
    """Bucket failures by normalized phrase. Used by both the in-memory
    and combined variants."""
    import re
    if not fails:
        return []
    buckets: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for r in fails:
        rid = r.get("request_id") or ""
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        raw = (r.get("question_truncated") or "").strip()
        if not raw:
            continue
        key = re.sub(r"\s+", " ", raw.lower())[:200]
        b = buckets.setdefault(key, {
            "phrase": raw, "count": 0,
            "kinds": Counter(), "latest_intent": None,
            "latest_ts": None,
        })
        b["count"] += 1
        kind = r.get("failure_kind") or "?"
        b["kinds"][kind] += 1
        intent = r.get("intent") or "?"
        ts = r.get("ts") or ""
        if not b["latest_ts"] or ts > b["latest_ts"]:
            b["latest_ts"] = ts
            b["latest_intent"] = intent
    rows = []
    for b in buckets.values():
        rows.append({
            "phrase": b["phrase"],
            "count": b["count"],
            "kinds": dict(b["kinds"]),
            "latest_intent": b["latest_intent"],
            "latest_ts": b["latest_ts"],
        })
    rows.sort(key=lambda r: (-r["count"], r["latest_ts"] or ""))
    return rows[:top_n]


def top_failed_phrases(top_n: int = 10) -> list[dict]:
    """Sprint 14: aggregate failed queries by normalized text. Returns
    [{phrase, count, kinds, latest_intent}] sorted by count desc.

    In-memory-ring only. For the admin server (separate process), use
    `top_failed_phrases_combined` which also reads on-disk JSONL."""
    with _ring_lock:
        records = list(_ring)
    fails = [r for r in records if r.get("is_failure")]
    return _aggregate_phrases(fails, top_n=top_n)


def _reset() -> None:
    """For tests."""
    with _ring_lock:
        _ring.clear()


# =====================================================================
# v5 Phase 0 — RequestTrace
# =====================================================================
#
# Append-only object threaded through the pipeline. Every layer:
#   trace.set_intent(...)
#   trace.add_entity_resolve(...)
#   trace.set_plan(...)
#   trace.add_tool_execution(...)
#   trace.set_render(...)
#   trace.set_critic(...)
#   trace.set_audit(...)
# then `trace.finalize()` writes a JSONL line and updates the ring.
#
# Phase 0: not yet wired into rag_v2.ask/ask_stream. Tests exercise the
# trace API; production keeps using `log_request` until Phase 5/6.
#
# The trace dict layout intentionally maps to the existing `log_request`
# fields so the status dashboard / admin /failed page keep working
# without changes. Extra fields (entity_resolves, render_view_type, ...)
# are additive and ignored by the existing aggregator.


@dataclass
class _ToolExecutionLog:
    tool: str
    args_summary: dict = field(default_factory=dict)
    runtime_ms: int = 0
    ok: bool = True
    cache_hit: bool = False
    data_validity: str | None = None     # ok / partial / empty_expected / empty_unexpected / broken
    err_type: str | None = None
    coverage_books_matched: int | None = None


@dataclass
class _EntityResolveLog:
    entity_type: str            # "author" / "book" / "word"
    query: str
    decision: str               # "resolved" / "clarify_needed" / "not_found"
    resolved: str | None
    confidence: float
    candidates: list[dict] = field(default_factory=list)
    normalization_trace: list[str] = field(default_factory=list)


@dataclass
class RequestTrace:
    """Per-request append-only trace. v5 Phase 0 — wired manually in
    tests; Phase 4-5 will plug it into rag_v2.ask/ask_stream.
    """
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts_start: float = field(default_factory=time.perf_counter)
    ts_iso: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    query_raw: str = ""
    query_normalized: str | None = None
    engine: str = "v2"
    pipeline_version: str = "v5-foundation"

    # Layer slots
    intent: str | None = None
    intent_confidence: float | None = None
    intent_path: str | None = None         # "rules_fast" / "llm_planner" / "followup"

    entity_resolves: list[_EntityResolveLog] = field(default_factory=list)

    plan_template: str | None = None        # name from PLAN_TEMPLATES (Phase 4)
    plan_steps: list[dict] = field(default_factory=list)
    plan_clarify: bool = False
    plan_out_of_scope: bool = False

    tool_executions: list[_ToolExecutionLog] = field(default_factory=list)

    render_view_type: str | None = None
    render_phase_a_ms: int = 0
    render_phase_b_ms: int = 0
    render_phase_b_used: bool = False
    render_skeleton_chars: int = 0
    render_prose_chars: int = 0

    critic_verified: bool | None = None
    critic_unsupported_n: int = 0
    critic_flagged: bool = False

    audit_mismatches_n: int = 0

    budget_wall_clock_s_used: float = 0.0
    budget_wall_clock_s_max: float = 60.0
    budget_exceeded: bool = False

    # Final outcome
    answer_truncated: str | None = None
    is_failure: bool = False
    failure_kind: str | None = None
    error: str | None = None

    # ---- mutators (append-only) ----

    def set_query(self, raw: str, normalized: str | None = None) -> None:
        self.query_raw = raw
        if normalized is not None:
            self.query_normalized = normalized

    def set_intent(self, label: str, *, confidence: float | None = None,
                   path: str | None = None) -> None:
        self.intent = label
        if confidence is not None:
            self.intent_confidence = confidence
        if path is not None:
            self.intent_path = path

    def add_entity_resolve(self, *, entity_type: str, query: str,
                           decision: str, resolved: str | None = None,
                           confidence: float = 0.0,
                           candidates: list[dict] | None = None,
                           normalization_trace: list[str] | None = None) -> None:
        self.entity_resolves.append(_EntityResolveLog(
            entity_type=entity_type, query=query,
            decision=decision, resolved=resolved,
            confidence=confidence,
            candidates=candidates or [],
            normalization_trace=normalization_trace or [],
        ))

    def set_plan(self, *, template: str | None = None,
                 steps: list[dict] | None = None,
                 clarify: bool = False,
                 out_of_scope: bool = False) -> None:
        if template is not None:
            self.plan_template = template
        if steps is not None:
            self.plan_steps = steps
        self.plan_clarify = clarify
        self.plan_out_of_scope = out_of_scope

    def add_tool_execution(self, *, tool: str, args_summary: dict | None = None,
                           runtime_ms: int = 0, ok: bool = True,
                           cache_hit: bool = False,
                           data_validity: str | None = None,
                           err_type: str | None = None,
                           coverage_books_matched: int | None = None) -> None:
        self.tool_executions.append(_ToolExecutionLog(
            tool=tool,
            args_summary=args_summary or {},
            runtime_ms=runtime_ms,
            ok=ok,
            cache_hit=cache_hit,
            data_validity=data_validity,
            err_type=err_type,
            coverage_books_matched=coverage_books_matched,
        ))

    def set_render(self, *, view_type: str | None = None,
                   phase_a_ms: int = 0, phase_b_ms: int = 0,
                   phase_b_used: bool = False,
                   skeleton_chars: int = 0, prose_chars: int = 0) -> None:
        if view_type is not None:
            self.render_view_type = view_type
        self.render_phase_a_ms = phase_a_ms
        self.render_phase_b_ms = phase_b_ms
        self.render_phase_b_used = phase_b_used
        self.render_skeleton_chars = skeleton_chars
        self.render_prose_chars = prose_chars

    def set_critic(self, *, verified: bool | None = None,
                   unsupported_n: int = 0, flagged: bool = False) -> None:
        self.critic_verified = verified
        self.critic_unsupported_n = unsupported_n
        self.critic_flagged = flagged

    def set_audit(self, *, mismatches_n: int = 0) -> None:
        self.audit_mismatches_n = mismatches_n

    def set_budget(self, *, used_s: float, max_s: float | None = None,
                   exceeded: bool = False) -> None:
        self.budget_wall_clock_s_used = used_s
        if max_s is not None:
            self.budget_wall_clock_s_max = max_s
        self.budget_exceeded = exceeded

    def set_answer(self, text: str | None, *, max_chars: int = 500) -> None:
        if text is None:
            self.answer_truncated = None
            return
        s = text.strip()
        self.answer_truncated = s if len(s) <= max_chars else s[:max_chars] + "…"

    def mark_failure(self, kind: str, error: str | None = None) -> None:
        self.is_failure = True
        self.failure_kind = kind
        if error is not None:
            self.error = error

    # ---- finalize ----

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.ts_start) * 1000)

    def to_flat_log(self) -> dict:
        """Convert to the flat shape `log_request` expects, so the
        existing aggregator / status card / admin /failed page keep
        working. Extra v5 fields are additive — old consumers ignore."""
        d: dict[str, Any] = {
            "ts": self.ts_iso,
            "request_id": self.trace_id,
            "question_truncated": self.query_raw[:200] if self.query_raw else "",
            "intent": self.intent,
            "intent_confidence": self.intent_confidence,
            "engine": self.engine,
            "pipeline_version": self.pipeline_version,
            "total_elapsed_ms": self.elapsed_ms(),
            "tool_calls": [
                {
                    "name": e.tool,
                    "runtime_ms": e.runtime_ms,
                    "cache_hit": e.cache_hit,
                    "ok": e.ok,
                    "data_validity": e.data_validity,
                }
                for e in self.tool_executions
            ],
            "critic_verified": self.critic_verified,
            "critic_unsupported_n": self.critic_unsupported_n,
            "is_failure": self.is_failure,
            "failure_kind": self.failure_kind,
            "answer_truncated": self.answer_truncated,
            # v5-only fields (additive):
            "v5_intent_path": self.intent_path,
            "v5_plan_template": self.plan_template,
            "v5_plan_clarify": self.plan_clarify,
            "v5_plan_out_of_scope": self.plan_out_of_scope,
            "v5_entity_resolves": [
                {
                    "entity_type": r.entity_type,
                    "query": r.query,
                    "decision": r.decision,
                    "resolved": r.resolved,
                    "confidence": r.confidence,
                    "candidates_n": len(r.candidates),
                    "normalization": r.normalization_trace,
                }
                for r in self.entity_resolves
            ],
            "v5_render_view_type": self.render_view_type,
            "v5_render_phase_a_ms": self.render_phase_a_ms,
            "v5_render_phase_b_ms": self.render_phase_b_ms,
            "v5_render_phase_b_used": self.render_phase_b_used,
            "v5_audit_mismatches_n": self.audit_mismatches_n,
            "v5_budget_used_s": self.budget_wall_clock_s_used,
            "v5_budget_exceeded": self.budget_exceeded,
        }
        if self.error:
            d["error"] = self.error
        return {k: v for k, v in d.items() if v is not None and v != []}

    def finalize(self) -> dict:
        """Write the trace to the JSONL log + ring buffer via the
        existing `log_request` plumbing, and return the flat dict."""
        flat = self.to_flat_log()
        log_request(flat)
        return flat


def start_trace(query: str, *, engine: str = "v2") -> RequestTrace:
    """Convenience constructor — used by ask/ask_stream once Phase 4-5
    plugs the trace into the pipeline."""
    t = RequestTrace(engine=engine)
    t.set_query(query)
    return t


# Module-level marker for v5 readiness checks
V5_OBSERVABILITY_VERSION = "0.1"
