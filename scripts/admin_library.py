"""Admin library helpers — list / inspect / download / delete / re-process
user-uploaded books.

Mounted via admin_server.py routes. Files lived in:
  /workspace/raw_text/u<N>.txt                  — raw text (header-stripped)
  /workspace/spgc/user_tokens/U<N>_tokens.txt   — one-token-per-line dump
  /workspace/spgc/user_counts/U<N>_counts.txt   — word\tcount\n per line
  /workspace/spgc/derived/user_uploads_metadata.csv — DC metadata row

Stays out of admin_server.py to keep that file scannable.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RAW_DIR        = Path("/workspace/raw_text")
USER_TOKENS    = Path("/workspace/spgc/user_tokens")
USER_COUNTS    = Path("/workspace/spgc/user_counts")
USER_META      = Path("/workspace/spgc/derived/user_uploads_metadata.csv")
SCRIPTS_DIR    = Path("/workspace/scripts")
TOKENIZE_SCRIPT = SCRIPTS_DIR / "tokenize_user_books.py"

# Project Gutenberg header/footer markers — used by reprocess
_GUTENBERG_HEADER = re.compile(
    r"\*\*\* START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*",
    re.IGNORECASE | re.DOTALL,
)
_GUTENBERG_FOOTER = re.compile(
    r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG.*",
    re.IGNORECASE | re.DOTALL,
)

# u-id ↔ canonical case — admin path can come as "U7" or "u7", normalize.
_U_ID_RE = re.compile(r"^[Uu](\d+)$")


def normalize_uid(raw: str) -> str | None:
    """Return canonical 'U<N>' or None if not a valid user-id."""
    if not raw:
        return None
    m = _U_ID_RE.match(raw.strip())
    if not m:
        return None
    return f"U{int(m.group(1))}"


def _meta_rows() -> list[dict]:
    """Yield every row from user_uploads_metadata.csv (newest last)."""
    if not USER_META.exists():
        return []
    try:
        with open(USER_META, encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def _raw_path(u_id: str) -> Path:
    return RAW_DIR / f"{u_id.lower()}.txt"


def _tokens_path(u_id: str) -> Path:
    return USER_TOKENS / f"{u_id}_tokens.txt"


def _counts_path(u_id: str) -> Path:
    return USER_COUNTS / f"{u_id}_counts.txt"


# ---------- listing ----------

def list_user_books() -> list[dict]:
    """Inventory of every uploaded book + filesystem health.

    Row shape (one per known u-id):
      id, title, author, language, uploaded_ts, source_filename,
      raw_bytes, raw_present, tokens_present, counts_present,
      health: 'ok' / 'no_tokens' / 'no_raw' / 'truncated'
    """
    rows: list[dict] = []
    meta = {r["id"]: r for r in _meta_rows() if r.get("id")}

    # Build the union of metadata + filesystem-present ids
    fs_ids: set[str] = set()
    if RAW_DIR.exists():
        for p in RAW_DIR.glob("u*.txt"):
            stem = p.stem.upper()
            if stem.startswith("U") and stem[1:].isdigit():
                fs_ids.add(stem)
    all_ids = sorted(set(meta) | fs_ids,
                     key=lambda x: (int(x[1:]) if x[1:].isdigit() else 0))

    for u_id in all_ids:
        m = meta.get(u_id, {})
        raw = _raw_path(u_id)
        tok = _tokens_path(u_id)
        cnt = _counts_path(u_id)
        raw_present = raw.exists()
        tok_present = tok.exists()
        cnt_present = cnt.exists()
        raw_bytes = raw.stat().st_size if raw_present else 0

        # Health rules — order matters; first match wins
        if not raw_present:
            health = "no_raw"
        elif raw_bytes < 5000:
            # SPGC books cap ~10KB minimum content; <5KB is almost
            # certainly bad upload or stripped to nothing.
            health = "truncated"
        elif not tok_present:
            health = "no_tokens"
        elif not cnt_present:
            health = "no_counts"
        else:
            health = "ok"

        rows.append({
            "id":              u_id,
            "title":           m.get("title", "") or "",
            "author":          m.get("author", "") or "",
            "language":        m.get("language", "") or "",
            "year":            m.get("authoryearofbirth", "") or "",
            "uploaded_ts":     m.get("uploaded_ts", "") or "",
            "source_filename": m.get("source_filename", "") or "",
            "raw_bytes":       raw_bytes,
            "raw_present":     raw_present,
            "tokens_present":  tok_present,
            "counts_present":  cnt_present,
            "health":          health,
        })
    return rows


# ---------- per-book stats ----------

def book_stats(u_id: str) -> dict:
    """Detailed stats: word count, vocab size, top words, sample paragraphs."""
    u_id = normalize_uid(u_id) or ""
    if not u_id:
        return {"error": "invalid_id"}
    raw = _raw_path(u_id)
    tok = _tokens_path(u_id)
    cnt = _counts_path(u_id)
    if not raw.exists():
        return {"error": "raw_not_found", "id": u_id}

    raw_size = raw.stat().st_size
    raw_mtime = datetime.fromtimestamp(
        raw.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

    # Token count + vocab from counts file (much faster than re-tokenizing)
    total_tokens = 0
    vocab_size = 0
    top_words: list[dict] = []
    if cnt.exists():
        try:
            with open(cnt, encoding="utf-8") as fh:
                rows = []
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    w, c = parts[0], parts[1]
                    try:
                        c = int(c)
                    except ValueError:
                        continue
                    rows.append((w, c))
                    total_tokens += c
                    vocab_size += 1
            rows.sort(key=lambda x: x[1], reverse=True)
            top_words = [{"word": w, "count": c} for w, c in rows[:30]]
        except OSError:
            pass

    # Sample paragraphs — first 3 non-trivial paragraphs from raw
    sample_paragraphs: list[str] = []
    try:
        with open(raw, encoding="utf-8", errors="replace") as fh:
            content = fh.read(50_000)  # first 50KB only
        paras = re.split(r"\n\s*\n", content)
        for p in paras:
            p = p.strip()
            if 100 <= len(p) <= 600:
                sample_paragraphs.append(p)
                if len(sample_paragraphs) >= 3:
                    break
    except OSError:
        pass

    # Header detection — if Gutenberg header still present, flag it
    has_gutenberg_header = bool(_GUTENBERG_HEADER.search(content)) if 'content' in locals() else False
    has_gutenberg_footer = False
    try:
        with open(raw, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-20_000:]
        has_gutenberg_footer = bool(_GUTENBERG_FOOTER.search(tail))
    except OSError:
        pass

    return {
        "id":             u_id,
        "raw_bytes":      raw_size,
        "raw_mtime":      raw_mtime,
        "total_tokens":   total_tokens,
        "vocab_size":     vocab_size,
        "type_token_ratio": (vocab_size / total_tokens) if total_tokens else 0,
        "top_words":      top_words,
        "sample_paragraphs": sample_paragraphs,
        "tokens_present": tok.exists(),
        "counts_present": cnt.exists(),
        "has_gutenberg_header": has_gutenberg_header,
        "has_gutenberg_footer": has_gutenberg_footer,
    }


# ---------- raw download ----------

def book_raw_path(u_id: str) -> Path | None:
    u_id = normalize_uid(u_id) or ""
    if not u_id:
        return None
    p = _raw_path(u_id)
    return p if p.exists() else None


# ---------- delete ----------

def delete_book(u_id: str) -> dict:
    """Remove raw / tokens / counts / metadata row for a u-id.

    Chroma chunks live in a separate persistent index — they get
    reclaimed on the next reindex. For immediate cleanup, the admin
    can manually trigger reindex after delete.
    """
    u_id = normalize_uid(u_id) or ""
    if not u_id:
        return {"error": "invalid_id"}

    removed = []
    for p in [_raw_path(u_id), _tokens_path(u_id), _counts_path(u_id)]:
        if p.exists():
            try:
                p.unlink()
                removed.append(str(p))
            except OSError as e:
                return {"error": f"failed to remove {p}: {e}"}

    # Remove metadata row
    meta_changed = False
    if USER_META.exists():
        rows = _meta_rows()
        keep = [r for r in rows if r.get("id") != u_id]
        if len(keep) != len(rows):
            try:
                from .admin_server import USER_META_COLS  # type: ignore
            except Exception:
                # Fall back to header from current file
                with open(USER_META, encoding="utf-8") as fh:
                    USER_META_COLS = next(csv.reader(fh))
            with open(USER_META, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=USER_META_COLS)
                w.writeheader()
                for r in keep:
                    w.writerow({k: r.get(k, "") for k in USER_META_COLS})
            meta_changed = True

    return {
        "id":          u_id,
        "removed":     removed,
        "metadata_row_removed": meta_changed,
        "note": "Chroma index chunks remain until next reindex.",
    }


# ---------- reprocess ----------

def reprocess_book(u_id: str) -> dict:
    """Re-strip Gutenberg header/footer and re-tokenize.

    Use when upload landed with mangled markup or kept the boilerplate.
    Re-runs tokenize_user_books.py for the single book.
    """
    u_id = normalize_uid(u_id) or ""
    if not u_id:
        return {"error": "invalid_id"}
    raw = _raw_path(u_id)
    if not raw.exists():
        return {"error": "raw_not_found", "id": u_id}

    # Re-strip headers in-place (idempotent — if already clean, no-op)
    try:
        original = raw.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"error": f"read failed: {e}"}
    stripped = original
    head_match = _GUTENBERG_HEADER.search(stripped)
    if head_match:
        stripped = stripped[head_match.end():]
    foot_match = _GUTENBERG_FOOTER.search(stripped)
    if foot_match:
        stripped = stripped[:foot_match.start()]
    stripped = stripped.strip()

    if stripped != original.strip():
        try:
            raw.write_text(stripped, encoding="utf-8")
        except OSError as e:
            return {"error": f"write failed: {e}"}

    # Re-tokenize (kicks off the single-book tokenize script). Run inline
    # — this is admin-only, low traffic, and we want the result immediately.
    if not TOKENIZE_SCRIPT.exists():
        return {
            "id": u_id,
            "raw_size_before": len(original),
            "raw_size_after":  len(stripped),
            "header_stripped": bool(head_match),
            "footer_stripped": bool(foot_match),
            "tokenize_skipped": "tokenize_user_books.py not found",
        }
    try:
        result = subprocess.run(
            ["python", str(TOKENIZE_SCRIPT), "--id", u_id],
            capture_output=True, text=True, timeout=300,
        )
        ok = result.returncode == 0
    except Exception as e:
        return {"error": f"tokenize subprocess failed: {e}"}

    return {
        "id":               u_id,
        "raw_size_before":  len(original),
        "raw_size_after":   len(stripped),
        "header_stripped":  bool(head_match),
        "footer_stripped":  bool(foot_match),
        "tokenize_ok":      ok,
        "tokenize_stderr":  result.stderr[-500:] if not ok else "",
    }


# ---------- bulk audit ----------

def audit_library() -> dict:
    """Summarize health across all uploaded books."""
    books = list_user_books()
    health_counts = Counter(b["health"] for b in books)
    total_bytes = sum(b["raw_bytes"] for b in books)
    return {
        "total":         len(books),
        "by_health":     dict(health_counts),
        "total_bytes":   total_bytes,
        "broken":        [b for b in books if b["health"] != "ok"][:50],
    }
