"""S6 webapp — xlsx export: tables[] → workbook bytes.

One sheet per table, sheet name derived from ``table["tool"]``:
truncated to Excel's 31-char limit, forbidden chars ``[ ] : * ? / \\``
replaced with ``_``, duplicates deduped with ``_2``/``_3``… suffixes
(the suffix itself fits inside the 31-char cap).

N1: ``rows`` arrive with native scalars (scripts/v2/table_extract.py),
so numeric cells land in Excel as numbers (``cell.data_type == "n"``) —
no ``astype(str)`` / stringification anywhere on this path.
"""
from __future__ import annotations

import io
import re

import pandas as pd

_FORBIDDEN = set("[]:*?/\\")
_MAX_SHEET = 31


def sheet_name(raw: str | None, used: set[str]) -> str:
    """Excel-safe, unique (case-insensitive — Excel treats 'A' == 'a')."""
    name = "".join("_" if c in _FORBIDDEN else c for c in (raw or "").strip())
    name = name.strip("'") or "sheet"
    name = name[:_MAX_SHEET]
    base, i = name, 2
    while name.lower() in used:
        suffix = f"_{i}"
        name = base[:_MAX_SHEET - len(suffix)] + suffix
        i += 1
    used.add(name.lower())
    return name


def tables_to_xlsx(tables: list[dict]) -> bytes:
    """Build the workbook in memory. Empty input → single empty sheet
    (openpyxl refuses a workbook with zero sheets)."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        used: set[str] = set()
        if not tables:
            pd.DataFrame().to_excel(writer, sheet_name="empty", index=False)
        for t in tables:
            df = pd.DataFrame(t.get("rows") or [],
                              columns=t.get("columns") or None)
            df.to_excel(writer, sheet_name=sheet_name(t.get("tool"), used),
                        index=False)
    return buf.getvalue()


def safe_filename(raw: str | None) -> str:
    """Sanitise the client-suggested download name (no path tricks)."""
    name = re.sub(r"[^\w.\- ]", "_", (raw or "").strip())
    while ".." in name:
        name = name.replace("..", "_")
    name = name.lstrip(".")
    if not name.lower().endswith(".xlsx"):
        name = (name or "wordcracker_export") + ".xlsx"
    return name
