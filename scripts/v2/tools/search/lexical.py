"""v2 lexical_search — BM25-ranked phrase / word match via SQLite FTS5.

Complements the semantic_search (ChromaDB) by getting exact-match queries
right: "find the word ajar in the corpus" returns books that literally
contain "ajar", not nearby concepts. The hybrid_search tool merges this
with semantic results via RRF.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

log = logging.getLogger("wordcracker.v2.tools.search.lexical")

# Default lives in the bind-mounted /data/spgc/derived volume so it survives
# container restarts. Override with WC_FTS_DB if hosting elsewhere.
FTS_DB_PATH = Path(os.environ.get("WC_FTS_DB",
                                  "/workspace/spgc/derived/v2_fts.sqlite"))

_conn: sqlite3.Connection | None = None
_conn_lock = threading.Lock()


def _connect() -> sqlite3.Connection | None:
    """Open a shared read-only connection. Returns None if index is missing."""
    global _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        if not FTS_DB_PATH.exists():
            return None
        try:
            _conn = sqlite3.connect(
                f"file:{FTS_DB_PATH}?mode=ro", uri=True,
                check_same_thread=False,
            )
            _conn.row_factory = sqlite3.Row
            return _conn
        except sqlite3.Error as e:
            log.warning("FTS open failed: %s", e)
            return None


def _close_for_test() -> None:
    global _conn
    with _conn_lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@tool(
    name="lexical_search",
    category="search",
    description=(
        "Точный поиск по корпусу через SQLite FTS5 (BM25). "
        "Для запросов вида «найди книги где упоминается X», "
        "«в каких книгах есть фраза \"Y\"». Возвращает PG id + snippet + rank."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query":   {"type": "string",
                        "description": "FTS5 syntax: «word», «\"phrase\"», «word1 AND word2», «word*»"},
            "k":       {"type": "integer", "description": "default 10"},
            "snippet_chars": {"type": "integer", "description": "default 200"},
        },
        "required": ["query"],
    },
    requires=["word"],
    cost="cheap",
    cacheable=True,
)
def lexical_search(query: str, k: int = 10,
                   snippet_chars: int = 200) -> ToolResult:
    conn = _connect()
    if conn is None:
        return ToolResult(
            ok=False, tool="lexical_search", query={"query": query, "k": k},
            data={"matches": []},
            warnings=[ToolWarning(
                code="fts_unavailable",
                message=(
                    f"FTS5 index not found at {FTS_DB_PATH}. "
                    f"Run scripts/v2/build_fts_index.py first."
                ),
            )],
            coverage=Coverage(books_matched=0, books_total=-1),
            error=None,
        )

    # FTS5's bm25() is negative-better; we sort ascending. The `snippet`
    # function highlights matches with brackets — we use plain []s so a
    # downstream renderer can post-process.
    sql = (
        "SELECT d.id, d.path, "
        "       bm25(documents_fts) AS score, "
        "       snippet(documents_fts, 0, '[', ']', '...', ?) AS snippet "
        "FROM documents_fts "
        "JOIN documents d ON d.rowid = documents_fts.rowid "
        "WHERE documents_fts MATCH ? "
        "ORDER BY score LIMIT ?"
    )
    # FTS5 snippet() needs a token count, not a char count. Approximate
    # 5 chars/token and clamp to FTS5's [1, 64] range.
    n_tokens = max(1, min(64, snippet_chars // 5))
    try:
        rows = conn.execute(sql, (n_tokens, query, k)).fetchall()
    except sqlite3.OperationalError as e:
        # malformed FTS5 query syntax → invalid_args
        return ToolResult.fail(
            tool="lexical_search", err_type="invalid_args",
            message=f"FTS5 query error: {e}", query={"query": query},
        )

    matches: list[dict[str, Any]] = []
    for row in rows:
        matches.append({
            "pg_id": row["id"],
            "score": float(row["score"]),     # negative; smaller = better
            "snippet": row["snippet"],
        })

    return ToolResult.success(
        tool="lexical_search",
        data={"query": query, "matches": matches, "k": k},
        coverage=Coverage(books_matched=len(matches), books_total=-1),
        warnings=[ToolWarning("no_matches", "FTS5 returned 0 results")]
                 if not matches else [],
        query={"query": query, "k": k, "snippet_chars": snippet_chars},
    )
