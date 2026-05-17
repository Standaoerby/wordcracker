"""SQLite store for v2 profiles — authors, books, lemmas.

A single DB file with three tables, keyed by author_slug / pg_id / lemma. The
value column holds a JSON blob (the full profile dict). corpus_version is
stored alongside so a corpus refresh invalidates stale entries.

Reads and writes are individually atomic; we don't need cross-table
transactions for these mostly-independent profile snapshots.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from scripts.v2.corpus_version import current_source_info

log = logging.getLogger("wordcracker.v2.profiles")

DB_PATH = Path(os.environ.get("WC_V2_PROFILES_DB", "/data/v2_profiles/profiles.sqlite"))

_DDL = """
CREATE TABLE IF NOT EXISTS author_profile (
    author_regex   TEXT PRIMARY KEY,
    corpus_version TEXT NOT NULL,
    payload        TEXT NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS book_profile (
    pg_id          TEXT PRIMARY KEY,
    corpus_version TEXT NOT NULL,
    payload        TEXT NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lemma_profile (
    lemma          TEXT PRIMARY KEY,
    corpus_version TEXT NOT NULL,
    payload        TEXT NOT NULL,
    updated_at     REAL NOT NULL
);
"""


_conn_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False,
                                    isolation_level=None)  # autocommit
            _conn.executescript(_DDL)
    return _conn


def _now() -> float:
    return time.time()


def get(table: str, key_col: str, key: str) -> dict | None:
    """Return profile dict if cached and corpus_version-fresh, else None.

    `stale_ok=True` would return blob anyway; not exposed yet — readers can
    extend later when we add a stale-tolerant rendering mode."""
    conn = _connect()
    row = conn.execute(
        f"SELECT corpus_version, payload, updated_at FROM {table} WHERE {key_col} = ?",
        (key,),
    ).fetchone()
    if not row:
        return None
    cv, payload, updated_at = row
    current_cv = current_source_info().corpus_version
    if cv != current_cv:
        return None
    try:
        d = json.loads(payload)
    except json.JSONDecodeError as e:
        log.warning("corrupt profile blob for %s=%s: %s", key_col, key, e)
        return None
    d["_cached_at"] = updated_at
    d["_corpus_version"] = cv
    return d


def put(table: str, key_col: str, key: str, payload: dict) -> None:
    conn = _connect()
    cv = current_source_info().corpus_version
    conn.execute(
        f"INSERT INTO {table} ({key_col}, corpus_version, payload, updated_at) "
        f"VALUES (?, ?, ?, ?) "
        f"ON CONFLICT({key_col}) DO UPDATE SET "
        f"corpus_version=excluded.corpus_version, "
        f"payload=excluded.payload, "
        f"updated_at=excluded.updated_at",
        (key, cv, json.dumps(payload, ensure_ascii=False, default=str), _now()),
    )


def stats() -> dict:
    """For status_server dashboard."""
    conn = _connect()
    out = {}
    for t in ("author_profile", "book_profile", "lemma_profile"):
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    return out


def clear() -> None:
    """For tests + manual ops."""
    conn = _connect()
    for t in ("author_profile", "book_profile", "lemma_profile"):
        conn.execute(f"DELETE FROM {t}")
