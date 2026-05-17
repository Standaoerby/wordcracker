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
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("wordcracker.v2.observability")

LOG_DIR = Path(os.environ.get("WC_V2_LOG_DIR", "/data/logs/v2"))
RING_SIZE = int(os.environ.get("WC_V2_RING_SIZE", "256"))

_ring: deque[dict] = deque(maxlen=RING_SIZE)
_ring_lock = threading.Lock()


def _today_log_path() -> Path:
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return LOG_DIR / f"queries-{d}.jsonl"


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


def _reset() -> None:
    """For tests."""
    with _ring_lock:
        _ring.clear()
