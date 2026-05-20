"""v2 lexical_search — BM25-ranked phrase / word match via SQLite FTS5.

Complements the semantic_search (ChromaDB) by getting exact-match queries
right: "find the word ajar in the corpus" returns books that literally
contain "ajar", not nearby concepts. The hybrid_search tool merges this
with semantic results via RRF.

Sprint 21 (Stan B100): each match now carries `title` and `author` when
v1 metadata can resolve the pg_id. Closes the «renderer echoes PG2554
verbatim instead of Crime and Punishment» bug. The lookup is mtime-cached
so the second query in the same session is ~0ms metadata cost.
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


# Sprint 21 B100 — pg_id → (title, author) lookup. Built lazily from
# v1 metadata DataFrame; lock for thread safety. The map is small
# (~77k rows × 3 fields = ~5 MB Python dict), so we keep it in
# memory once built. Rebuilds when metadata mtime changes — the
# v1 _metadata_df helper already mtime-caches its DataFrame, we
# just need to detect when our dict is stale.
_TITLE_CACHE: dict[str, dict[str, str]] | None = None
_TITLE_CACHE_LOCK = threading.Lock()
_TITLE_CACHE_KEY: tuple | None = None    # mtime-tuple of meta files


def _title_lookup() -> dict[str, dict[str, str]]:
    """Return {pg_id: {'title': ..., 'author': ...}}. Rebuilds when v1
    metadata mtime changes. Returns empty dict if v1 unavailable."""
    global _TITLE_CACHE, _TITLE_CACHE_KEY
    try:
        # v1 _metadata_df is mtime-cached; cheap repeat call.
        from scripts.rag_tools import (_metadata_df, SPGC_METADATA,
                                         USER_UPLOADS_META, ORPHAN_PG_META)
    except ImportError:
        return {}
    cur_key = tuple(
        p.stat().st_mtime if p.exists() else 0.0
        for p in (SPGC_METADATA, USER_UPLOADS_META, ORPHAN_PG_META)
    )
    with _TITLE_CACHE_LOCK:
        if _TITLE_CACHE is not None and _TITLE_CACHE_KEY == cur_key:
            return _TITLE_CACHE
        try:
            df = _metadata_df()
        except Exception as e:
            log.warning("title-lookup metadata load failed: %s", e)
            return _TITLE_CACHE or {}
        out: dict[str, dict[str, str]] = {}
        title_col = "title" if "title" in df.columns else None
        author_col = "author" if "author" in df.columns else None
        # Sprint 22 B4: also pull `language` so hybrid_search can post-
        # filter by lang_hint (Stan Round 12 Q5 — «английская классика»
        # surfaced Finnish/Hungarian/Italian books because no lang
        # filter was applied anywhere in the pipeline).
        lang_col = next((c for c in ("language", "lang") if c in df.columns),
                        None)
        id_col = "pg_id" if "pg_id" in df.columns else (
            "id" if "id" in df.columns else None)
        if id_col is None:
            return {}
        for _, row in df.iterrows():
            pid = str(row[id_col]) if row[id_col] is not None else ""
            if not pid:
                continue
            entry: dict[str, str] = {}
            if title_col:
                t = row.get(title_col)
                if t and not (isinstance(t, float) and t != t):  # not NaN
                    entry["title"] = str(t)
            if author_col:
                a = row.get(author_col)
                if a and not (isinstance(a, float) and a != a):
                    entry["author"] = str(a)
            if lang_col:
                lv = row.get(lang_col)
                if lv and not (isinstance(lv, float) and lv != lv):
                    # Normalize «en», «English», «en-US» → "en"
                    entry["language"] = str(lv).lower().strip().split("-")[0][:3]
            if entry:
                out[pid] = entry
        _TITLE_CACHE = out
        _TITLE_CACHE_KEY = cur_key
        return out

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
    # Sprint 21+ Stan B100: matches now include title/author via v1
    # metadata lookup. Old cached results without those fields stop
    # being served.
    wrapper_version="v2-titles",
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

    # Sprint 21 B100 — augment each match with title/author from metadata.
    # Renderer rule 16 says: prefer title in user-facing text. Without
    # this enrichment the renderer has no choice but to echo PG ids.
    lookup = _title_lookup()
    matches: list[dict[str, Any]] = []
    for row in rows:
        pid = row["id"]
        entry = {
            "pg_id": pid,
            "score": float(row["score"]),     # negative; smaller = better
            "snippet": row["snippet"],
        }
        meta = lookup.get(str(pid))
        if meta:
            if meta.get("title"):
                entry["title"] = meta["title"]
            if meta.get("author"):
                entry["author"] = meta["author"]
        matches.append(entry)

    return ToolResult.success(
        tool="lexical_search",
        data={"query": query, "matches": matches, "k": k},
        coverage=Coverage(books_matched=len(matches), books_total=-1),
        warnings=[ToolWarning("no_matches", "FTS5 returned 0 results")]
                 if not matches else [],
        query={"query": query, "k": k, "snippet_chars": snippet_chars},
    )
