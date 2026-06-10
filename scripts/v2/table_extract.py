"""S6 webapp — table extraction from ToolResult for the SSE/xlsx path.

Turns a live ToolResult into zero or more plain tables
``{"tool", "columns", "rows", "col_meta"?}`` for:
  - SSE ``table`` events + the ``tables[]`` done-envelope (api_loop),
  - the xlsx export (one sheet per table),
  - clickable cells on the frontend (B105, via ``col_meta``).

N1 invariant (plan.md §2.4): **every cell is a native scalar**
(int | float | str | bool | None). numpy/pandas scalars are unwrapped via
``.item()``; nested structures are flattened to a compact JSON string as a
last resort. Numbers must stay numbers all the way into the xlsx cell —
``json.dumps(default=str)`` downstream is only a safety net for genuinely
unserialisable values, never the serialisation path for numerics.

``TABLE_EXTRACTORS`` is a registry ``{tool_name: extractor}`` for tools
whose payload needs bespoke shaping; everything else goes through the
generic shape-sniffing extractor (list[dict] → table; dict → its list[dict]
values, else one row of its scalars).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

log = logging.getLogger("wordcracker.table_extract")

# Columns that mark a cell as dispatchable back into chat (B105).
# Backend has shipped title+pg_id together since B100.
_BOOK_ID_COLS = ("pg_id",)
_BOOK_LABEL_COLS = ("title", "book", "book_title")
_WORD_COLS = ("word", "token", "ngram", "lemma")


def _scalar(v: Any) -> Any:
    """Table cell → native scalar. Numbers stay numbers (N1): a stringified
    number turns the xlsx cell into text and breaks sorting/sums."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    item = getattr(v, "item", None)        # numpy.int64 / float32 / bool_
    if callable(item):
        try:
            return _scalar(v.item())
        except (ValueError, TypeError):
            pass
    if isinstance(v, (list, tuple, dict)):
        # Nested values should be flattened by a bespoke extractor; this
        # is the generic fallback so the cell is at least readable.
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _columns_of(records: list[dict]) -> list[str]:
    """Union of keys, ordered by first appearance. Private keys
    (``_render_note`` etc.) never reach the client."""
    cols: list[str] = []
    for rec in records:
        for k in rec:
            if isinstance(k, str) and k.startswith("_"):
                continue
            if k not in cols:
                cols.append(k)
    return cols


def _col_meta(columns: list[str]) -> dict | None:
    """B105 — mark columns whose cells dispatch a chat query on click.
    Optional: absent meta simply means no clickable cells."""
    meta: dict = {}
    has_book_id = any(c in columns for c in _BOOK_ID_COLS)
    if has_book_id:
        for c in columns:
            if c in _BOOK_LABEL_COLS:
                meta[c] = {"kind": "book", "id_col": "pg_id"}
                break
    for c in columns:
        if c in _WORD_COLS:
            meta[c] = {"kind": "word"}
            break
    return meta or None


def _is_record_list(v: Any) -> bool:
    return (isinstance(v, list) and len(v) > 0
            and all(isinstance(x, dict) for x in v))


def _table_from_records(tool: str, records: list[dict]) -> dict | None:
    columns = _columns_of(records)
    if not columns:
        return None
    table = {
        "tool": tool,
        "columns": columns,
        "rows": [[_scalar(rec.get(c)) for c in columns] for rec in records],
    }
    meta = _col_meta(columns)
    if meta:
        table["col_meta"] = meta
    return table


def _generic_extract(tool: str, data: Any) -> list[dict]:
    """Shape-sniffing fallback: covers the common v2 payload shapes."""
    if _is_record_list(data):
        t = _table_from_records(tool, data)
        return [t] if t else []
    if isinstance(data, dict):
        tables: list[dict] = []
        record_keys = [k for k, v in data.items()
                       if isinstance(k, str) and not k.startswith("_")
                       and _is_record_list(v)]
        for key in record_keys:
            name = tool if len(record_keys) == 1 else f"{tool}.{key}"
            t = _table_from_records(name, data[key])
            if t:
                tables.append(t)
        if tables:
            return tables
        # Flat scalar dict (e.g. per-author stats) → single-row table.
        scalars = {k: v for k, v in data.items()
                   if isinstance(k, str) and not k.startswith("_")
                   and (v is None or isinstance(v, (bool, int, float, str))
                        or callable(getattr(v, "item", None)))}
        if len(scalars) >= 2:
            t = _table_from_records(tool, [scalars])
            return [t] if t else []
    return []


# Registry for tools whose payload needs bespoke shaping. Keep empty until
# a real payload defeats the generic extractor — every entry must come with
# a test pinning the real tool output shape (CLAUDE.md R3/R4 spirit).
TABLE_EXTRACTORS: dict[str, Callable[[str, Any], list[dict]]] = {}


def extract_tables(tool: str, data: Any) -> list[dict]:
    """Public entry: ToolResult.tool + ToolResult.data → list of tables.
    Never raises — a table we failed to extract is a missing nicety, not a
    broken stream."""
    try:
        extractor = TABLE_EXTRACTORS.get(tool, _generic_extract)
        return extractor(tool, data)
    except Exception:
        log.exception("table extraction failed for tool=%s", tool)
        return []
