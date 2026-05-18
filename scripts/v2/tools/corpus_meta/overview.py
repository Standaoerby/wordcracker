"""v2 pilot tool: corpus_overview.

Refactor of scripts/rag_tools.py::corpus_overview into the v2 contract.
Same data shape as v1, returned inside a ToolResult envelope so the dispatcher,
cache layer, and observability can treat it uniformly with every other tool.

Reads:
  /workspace/raw_text/                       — current raw book pool
  /workspace/gutenberg_raw/                  — rsync mirror dump
  /workspace/spgc/derived/corpus_meta.json   — SPGC baseline numbers
  /workspace/spgc/derived/build_index*.log   — tqdm progress tail
  ChromaDB persistent client                 — chunk count (skipped if reindex active)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from scripts.v2.tool_registry import tool
from scripts.v2._types import Coverage, ToolResult, ToolWarning

CHROMA_PATH = os.environ.get("WC_CHROMA_PATH", "/workspace/chroma_db")
COLLECTION_NAME = os.environ.get("WC_CHROMA_COLLECTION", "gutenberg-index")
RAW_DIR = Path(os.environ.get("WC_RAW_TEXT_DIR", "/workspace/raw_text"))
RSYNC_DIR = Path(os.environ.get("WC_RSYNC_DIR", "/workspace/gutenberg_raw"))
DERIVED_DIR = Path(os.environ.get("WC_DERIVED_DIR", "/workspace/spgc/derived"))


_TQDM_RE = re.compile(
    r"books:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)\s*"
    r"\[([\d:]+)<([\d:?]+),\s*([\d.]+)\s*book/s\]"
)


@tool(
    name="corpus_overview",
    category="corpus_meta",
    description=(
        "Сколько всего книг в базе, сколько чанков в ChromaDB, какие источники, "
        "сводка SPGC baseline и текущий прогресс индексации. "
        "Используй для «сколько книг в базе», «что у тебя за корпус», «прогресс индексации»."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    requires=[],
    cost="cheap",
    cacheable=False,  # this tool is intentionally live — caching defeats the point
)
def corpus_overview() -> ToolResult:
    warnings: list[ToolWarning] = []
    data: dict = {}

    # raw_text
    pg_count = user_count = None
    if RAW_DIR.exists():
        try:
            pg_count = sum(1 for _ in RAW_DIR.glob("pg*.txt"))
            user_count = sum(1 for _ in RAW_DIR.glob("u*.txt"))
            data["raw_books_available"] = pg_count + user_count
            data["raw_books_pg"] = pg_count
            data["raw_books_user_uploads"] = user_count
        except OSError as e:
            warnings.append(ToolWarning("raw_dir_unreadable", str(e)))
    else:
        warnings.append(ToolWarning(
            "raw_dir_missing", f"{RAW_DIR} not found (running outside container?)"
        ))

    # rsync mirror dump
    if RSYNC_DIR.exists():
        try:
            data["rsync_mirror_files"] = sum(1 for _ in RSYNC_DIR.rglob("*-0.txt"))
        except OSError:
            pass

    data["rsync_running"] = _pgrep_alive("rsync.*gutenberg")

    # ChromaDB chunk count — skip during reindex (hnsw segments racing)
    reindex_active = _pgrep_alive("build_index_raw")
    if reindex_active:
        data["chromadb_chunks"] = "indexing in progress, count unavailable"
        warnings.append(ToolWarning(
            "reindex_active", "ChromaDB chunk count skipped: build_index_raw is running"
        ))
    else:
        try:
            import chromadb
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            col = client.get_collection(COLLECTION_NAME)
            data["chromadb_chunks"] = col.count()
        except Exception as e:
            warnings.append(ToolWarning("chromadb_unreachable", str(e)))

    # tqdm progress tail (most recent build_index*.log)
    progress = _read_reindex_progress()
    if progress:
        data["reindex_progress"] = progress["progress"]
        data["last_index_log"] = progress["log_name"]
        data["last_index_log_mtime"] = progress["mtime"]
    data["reindex_running"] = reindex_active

    # gap approx (chunks / 125 ≈ books indexed)
    chunks = data.get("chromadb_chunks")
    if isinstance(chunks, int) and isinstance(data.get("raw_books_available"), int):
        approx_indexed = max(1, chunks // 125)
        data["index_gap_approx"] = max(0, data["raw_books_available"] - approx_indexed)
        # Explicit summary fields the LLM can quote directly without doing
        # math itself. Q1 from Stan's 2026-05-18 demon round caught the
        # render saying «100% покрытие индекса» right next to «не вошло
        # 24 206 книг» — LLM was conflating FTS5 (covers all 55k) with
        # ChromaDB (English only). Spelling it out keeps the answer
        # truthful.
        data["semantic_index_books_approx"] = approx_indexed
        data["semantic_index_coverage_pct"] = round(
            100 * approx_indexed / data["raw_books_available"], 1)
        # Hint for the LLM renderer — explicit instruction in data form so
        # the Modelfile prompt doesn't have to know about the two indexes.
        data["_render_note"] = (
            "Корпус имеет два независимых индекса: ChromaDB semantic "
            "(только English, ~50% книг) и SQLite FTS5 lexical (вся "
            "коллекция). Не объединяй покрытие двух индексов в один "
            "процент — это разные кадры. Если пользователь спросил про "
            "конкретный индекс, отвечай только про него."
        )

    # SPGC baseline (frozen 2018-07 dump)
    meta_path = DERIVED_DIR / "corpus_meta.json"
    if meta_path.exists():
        try:
            data["spgc_baseline"] = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            warnings.append(ToolWarning("spgc_meta_unreadable", str(e)))

    data["sources"] = [
        "rsync mirror ftp.ibiblio.org (current)",
        "per-author direct download from gutenberg.org/cache/epub",
        "PG DVD July 2006 local copy",
        "user uploads (admin endpoint, EPUB → tokens)",
    ]

    return ToolResult.success(
        tool="corpus_overview",
        data=data,
        warnings=warnings,
        coverage=Coverage(
            books_matched=data.get("raw_books_available", -1),
            books_total=data.get("raw_books_available", -1),
        ),
    )


def _pgrep_alive(pattern: str) -> bool:
    try:
        proc = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5,
        )
        return bool(proc.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _read_reindex_progress() -> dict | None:
    if not DERIVED_DIR.exists():
        return None
    try:
        logs = sorted(
            DERIVED_DIR.glob("build_index*.log"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except OSError:
        return None
    if not logs:
        return None
    log_path = logs[0]
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="ignore").replace("\r", "\n")
    except OSError:
        return None
    last = None
    for line in tail.splitlines():
        m = _TQDM_RE.search(line)
        if m:
            last = m
    if not last:
        return {
            "log_name": log_path.name,
            "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(log_path.stat().st_mtime)),
            "progress": None,
        }
    return {
        "log_name": log_path.name,
        "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(log_path.stat().st_mtime)),
        "progress": {
            "percent": int(last.group(1)),
            "books_done": int(last.group(2)),
            "books_total": int(last.group(3)),
            "elapsed": last.group(4),
            "eta_remaining": last.group(5),
            "rate_book_per_s": float(last.group(6)),
        },
    }
