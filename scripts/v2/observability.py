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
