#!/usr/bin/env python3
"""
wordcracker status dashboard — pure-stdlib HTTP server.

Reads derived/ artifacts and filesystem state, renders a small dashboard
plus a JSON endpoint. No deps beyond Python 3 stdlib so it can run
straight on the host without pip.

Run:
  nohup python3 status_server.py --port 8889 > status_server.log 2>&1 &

URLs:
  /              dashboard HTML (auto-refresh every 30 s)
  /api/status    same data as JSON
  /health        "ok"
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DERIVED = Path("/data/spgc/derived")
TOKENS_DIR = Path("/data/spgc/SPGC-tokens-2018-07-18")
COUNTS_DIR = Path("/data/spgc/SPGC-counts-2018-07-18")
METADATA_CSV = Path("/data/spgc/SPGC-metadata-2018-07-18.csv")
RAW_TEXT = Path("/data/raw_text")
WODEHOUSE_RAW = Path("/data/wodehouse_raw")
GUTENBERG_RAW = Path("/data/gutenberg_raw")
CHROMA_DB = Path("/data/chroma_db")
BUILD_INDEX_LOG = DERIVED / "build_index.log"


def safe_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def safe_lines(path: Path):
    try:
        with open(path) as fh:
            return sum(1 for _ in fh)
    except Exception:
        return None


def dir_size_bytes(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def count_files(path: Path, pattern: str = "*") -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.glob(pattern))


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_int(n) -> str:
    if n is None:
        return "—"
    return f"{n:,}".replace(",", " ")


_top_authors_cache = {"ts": 0, "data": []}

def top_authors(limit: int = 20, lang_marker: str = "'en'", ttl: int = 300):
    """Top N authors in SPGC metadata.csv by book count, en only. Cached."""
    now = time.time()
    if _top_authors_cache["data"] and now - _top_authors_cache["ts"] < ttl:
        return _top_authors_cache["data"]
    if not METADATA_CSV.exists():
        return []
    books = Counter()
    downloads = Counter()
    try:
        with open(METADATA_CSV, encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            for row in rd:
                author = (row.get("author") or "").strip()
                if not author:
                    continue
                if lang_marker not in (row.get("language") or ""):
                    continue
                books[author] += 1
                try:
                    downloads[author] += int(row.get("downloads") or 0)
                except ValueError:
                    pass
    except Exception:
        return []
    out = [
        {"author": a, "books": n, "downloads": downloads[a]}
        for a, n in books.most_common(limit)
    ]
    _top_authors_cache["ts"] = now
    _top_authors_cache["data"] = out
    return out


def slug_for_author(author: str) -> str:
    """Mirrors the slug derivation in spgc_author_affinity.py: surname lowercased, non-alnum -> _."""
    surname = author.split(",", 1)[0].lower()
    return re.sub(r"[^a-z0-9]+", "_", surname).strip("_") or "author"


import threading

# Module-level TTL cache + double-checked locking. Without the cache, every HTTP
# hit (dashboard auto-refreshes every 30s, plus /api/status pollers) re-runs
# the full collect_status() — historically spawning a `docker exec` chromadb
# subprocess that piled up under concurrency and wedged the docker daemon.
#
# The lock prevents the cache-miss thundering-herd: when N concurrent requests
# all see an expired cache, only one of them runs the slow path; the rest wait
# on the lock and then return the freshly-cached value. Without DCL we'd burn
# N×CPU and N×SQLite reads on every cache miss under load.
_STATUS_CACHE: dict = {"data": None, "ts": 0.0}
_STATUS_TTL_SEC = 30.0
_STATUS_LOCK = threading.Lock()


def collect_status(force_fresh: bool = False):
    # Fast path: cache hit without acquiring the lock.
    now_ts = time.time()
    if (not force_fresh
            and _STATUS_CACHE["data"] is not None
            and (now_ts - _STATUS_CACHE["ts"]) < _STATUS_TTL_SEC):
        return _STATUS_CACHE["data"]

    # Slow path: only one thread enters at a time.
    with _STATUS_LOCK:
        # Double-check after acquiring the lock — another thread may have
        # populated the cache while we were waiting.
        now_ts = time.time()
        if (not force_fresh
                and _STATUS_CACHE["data"] is not None
                and (now_ts - _STATUS_CACHE["ts"]) < _STATUS_TTL_SEC):
            return _STATUS_CACHE["data"]
        return _build_status(now_ts)


def _build_status(now_ts: float):
    """Actual status assembly — runs under _STATUS_LOCK."""

    corpus_meta = safe_json(DERIVED / "corpus_meta.json")
    wode_meta   = safe_json(DERIVED / "wodehouse_affinity_meta.json")
    ner_meta    = safe_json(DERIVED / "wodehouse_ner_filter_meta.json")

    # rsync background job
    rsync_alive = False
    try:
        out = subprocess.run(["pgrep", "-af", "rsync.*gutenberg"], capture_output=True, text=True)
        rsync_alive = bool(out.stdout.strip()) and "rsync " in out.stdout
    except Exception:
        pass

    # consolidated raw text directory (hard-linked from sources)
    raw_files = count_files(RAW_TEXT, "*.txt")
    raw_bytes = dir_size_bytes(RAW_TEXT) if RAW_TEXT.exists() else 0
    # sources for provenance
    gb_raw_files = count_files(GUTENBERG_RAW, "**/*-0.txt") if GUTENBERG_RAW.exists() else 0
    wd_raw_files = count_files(WODEHOUSE_RAW, "*.txt")

    # author affinities present
    affinities = []
    if DERIVED.exists():
        for f in sorted(DERIVED.glob("*_affinity.csv")):
            slug = f.name.replace("_affinity.csv", "")
            lines = safe_lines(f)
            rows = (lines - 1) if lines else None
            meta = safe_json(DERIVED / f"{slug}_affinity_meta.json")
            affinities.append({
                "slug": slug,
                "rows": rows,
                "size": f.stat().st_size,
                "books_matched":    meta and meta.get("books_matched"),
                "books_aggregated": meta and meta.get("books_aggregated"),
                "author_tokens":    meta and meta.get("author_tokens"),
            })

    # context files
    ctx_dir = DERIVED / "contexts"
    contexts = count_files(ctx_dir, "*.txt")

    # chroma db
    chroma_bytes = dir_size_bytes(CHROMA_DB) if CHROMA_DB.exists() else 0
    build_running = False
    build_progress = None
    try:
        # Match python's running build_index_raw, not any shell watcher
        # whose command line just mentions the script name as a string.
        out = subprocess.run(["pgrep", "-af", "python.*build_index_raw"],
                             capture_output=True, text=True)
        build_running = bool(out.stdout.strip())
    except Exception:
        pass
    if BUILD_INDEX_LOG.exists():
        try:
            # last tqdm line in the log carries "N/Total" book progress
            tail = BUILD_INDEX_LOG.read_text(errors="ignore").splitlines()[-1] if BUILD_INDEX_LOG.stat().st_size else ""
            tail = tail.replace("\r", "\n").splitlines()[-1] if "\r" in tail else tail
            build_progress = tail[:160]
        except Exception:
            pass

    # ollama — alive? GPU? loaded models? VRAM in use by qwen3?
    ollama: dict = {"up": False, "gpu": False, "models_loaded": [], "error": None}
    try:
        proc = subprocess.run(
            ["docker", "exec", "ollama", "nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5,
        )
        ollama["gpu"] = (proc.returncode == 0)
        ollama["gpu_line"] = (proc.stdout.strip().splitlines() or [""])[0][:120]
    except Exception as e:
        ollama["error"] = f"nvidia-smi: {e}"
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=3) as r:
            data = json.loads(r.read())
            ollama["up"] = True
            for m in data.get("models", []):
                ollama["models_loaded"].append({
                    "name": m.get("name"),
                    "size_vram_mb": (m.get("size_vram") or 0) // (1024 * 1024),
                    "expires_at": m.get("expires_at"),
                })
    except Exception as e:
        ollama["error"] = (ollama["error"] or "") + f" | api/ps: {e}"
    try:
        gpu_q = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        used_mb, total_mb, util = [x.strip() for x in gpu_q.split(",")]
        ollama["host_vram_used_mb"]  = int(used_mb)
        ollama["host_vram_total_mb"] = int(total_mb)
        ollama["host_gpu_util_pct"]  = int(util)
    except Exception:
        pass

    # disk
    du = shutil.disk_usage("/data")

    # uptime
    try:
        with open("/proc/uptime") as fh:
            seconds = float(fh.read().split()[0])
        hours, rem = divmod(int(seconds), 3600)
        mins = rem // 60
        uptime = f"{hours}h {mins}m"
    except Exception:
        uptime = "?"

    result = {
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "spgc": {
            "tokens_dir":   str(TOKENS_DIR),
            "tokens_files": count_files(TOKENS_DIR, "*_tokens.txt"),
            "counts_files": count_files(COUNTS_DIR, "*_counts.txt"),
            "metadata_exists": METADATA_CSV.exists(),
        },
        "baseline": corpus_meta or {},
        "wodehouse": wode_meta or {},
        "ner_filter": ner_meta or {},
        "authors_analyzed": affinities,
        "raw_text": {
            "books": raw_files,
            "bytes": raw_bytes,
            "source_per_author":  wd_raw_files,
            "source_rsync_mirror": gb_raw_files,
            "rsync_running": rsync_alive,
        },
        "contexts_files": contexts,
        "ollama": ollama,
        "chroma": {
            "db_bytes": chroma_bytes,
            "build_running": build_running,
            "build_last_line": build_progress or "",
        },
        "disk_data": {
            "total": du.total,
            "used":  du.used,
            "free":  du.free,
            "pct_used": round(du.used / du.total * 100, 1),
        },
        "host": {
            "uptime": uptime,
        },
        "top_authors": top_authors(20),
        "health": _collect_health(ollama, chroma_bytes, build_running, du),
    }
    _STATUS_CACHE["data"] = result
    _STATUS_CACHE["ts"] = now_ts
    return result


def _check_http(url: str, timeout: float = 2.0) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _check_container_port(container: str, port: int) -> bool:
    """exec into container, hit local port — works for chat/admin on 127.0.0.1."""
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "python", "-c",
             f"import socket; s=socket.socket(); s.settimeout(2); "
             f"s.connect(('127.0.0.1', {port})); s.close()"],
            capture_output=True, timeout=5,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _systemd_active(unit: str) -> bool:
    try:
        proc = subprocess.run(["systemctl", "is-active", unit],
                              capture_output=True, text=True, timeout=3)
        return proc.stdout.strip() == "active"
    except Exception:
        return False


def _check_chromadb_via_sqlite(build_running: bool) -> tuple[bool, str]:
    """Read ChromaDB chunk count from its SQLite file on the host.

    Replaces the prior `docker exec python -c "import chromadb..."` subprocess,
    which under concurrent dashboard pollers piled up zombie processes inside
    the docker daemon, eventually wedging SSH and OOM-killing the host. Using
    SQLite read-only with a hard timeout never spawns a subprocess.
    """
    import sqlite3
    chroma_dir = Path("/data/chroma_db")
    if build_running:
        return False, "indexing in progress, count unavailable"
    if not chroma_dir.exists():
        return False, "/data/chroma_db missing"
    db = chroma_dir / "chroma.sqlite3"
    if not db.exists():
        # fall back to filesystem-size signal so the card stays informative
        size_gb = sum(f.stat().st_size for f in chroma_dir.rglob("*")
                      if f.is_file()) / 1e9
        return True, f"{size_gb:.1f} GB on disk (no SQLite metadata)"
    try:
        # Read-only, no journal contention. 2s timeout is generous for a
        # COUNT(*) on a 4-million-row table with an index.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
            n = int(row[0]) if row else 0
            return True, f"{n:,} chunks"
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # busy/locked means the indexer is writing; that's not a failure
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return True, "indexer writing (db locked, skipping count)"
        return False, f"sqlite: {str(e)[:80]}"
    except Exception as e:
        return False, f"sqlite read failed: {str(e)[:80]}"


def _collect_health(ollama: dict, chroma_bytes: int, build_running: bool, du) -> dict:
    """Aggregate health snapshot for the big overview card."""
    components: list[dict] = []

    # 1) Ollama + GPU
    ollama_ok = bool(ollama.get("up") and ollama.get("gpu") and ollama.get("models_loaded"))
    components.append({
        "name": "Ollama LLM + GPU", "ok": ollama_ok,
        "detail": ("qwen3:14b in VRAM" if ollama_ok else "API or GPU degraded"),
    })

    # 2) ChromaDB readable — read its SQLite metadata directly from the host
    # filesystem (no docker exec, no subprocess hangs). The persistent client
    # path /data/chroma_db is bind-mounted into the container and the SQLite
    # file holds the chunk row count we want.
    chroma_ok, chroma_detail = _check_chromadb_via_sqlite(build_running)
    components.append({"name": "ChromaDB index", "ok": chroma_ok, "detail": chroma_detail})

    # 3) chat_server
    chat_ok = _check_container_port("wordcracker-gutenberg-lab-1", 8890)
    components.append({"name": "Chat server (:8890)", "ok": chat_ok,
                       "detail": "port reachable in container" if chat_ok else "not listening"})

    # 4) admin_server
    admin_ok = _check_container_port("wordcracker-gutenberg-lab-1", 8891)
    components.append({"name": "Admin server (:8891)", "ok": admin_ok,
                       "detail": "port reachable in container" if admin_ok else "not listening"})

    # 5) cloudflared
    cf_ok = _systemd_active("cloudflared")
    components.append({"name": "Cloudflare tunnel", "ok": cf_ok,
                       "detail": "systemd active" if cf_ok else "service down"})

    # 6) nginx
    ng_ok = _systemd_active("nginx")
    components.append({"name": "nginx reverse-proxy", "ok": ng_ok,
                       "detail": "systemd active" if ng_ok else "service down"})

    # 7) disk
    pct = round(du.used / du.total * 100, 1)
    disk_ok = pct < 90
    components.append({"name": "Disk /data", "ok": disk_ok,
                       "detail": f"{pct}% used ({human_bytes(du.free)} free)"})

    # Roll-up. ChromaDB intentionally NOT critical — only semantic_search/contexts
    # tools depend on it, the agentic flow keeps working on counts/affinity/etc.
    # docker-exec count() also flaps occasionally on cold cache → would falsely
    # downgrade overall to "down" on a transient timeout.
    critical = ["Ollama LLM + GPU", "Chat server (:8890)", "Cloudflare tunnel",
                "nginx reverse-proxy"]
    crit_failed = [c for c in components if c["name"] in critical and not c["ok"]]
    non_crit_failed = [c for c in components if c["name"] not in critical and not c["ok"]]

    if crit_failed:
        overall = "down"
    elif non_crit_failed or build_running:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "overall":     overall,
        "components":  components,
        "failed":      [c["name"] for c in components if not c["ok"]],
    }


def _health_card_html(h: dict) -> str:
    overall = h.get("overall", "unknown")
    color = {"healthy": "#7ed321", "degraded": "#f5a623", "down": "#e05a5a"}.get(overall, "#888")
    label = {"healthy": "HEALTHY ✅", "degraded": "DEGRADED ⚠", "down": "DOWN ❌"}.get(overall, overall.upper())
    rows = ""
    for c in h.get("components", []):
        ico = "🟢" if c["ok"] else "🔴"
        rows += (f"<tr><td style='width:22px'>{ico}</td>"
                 f"<td><b>{c['name']}</b></td>"
                 f"<td style='color:#888'>{c['detail']}</td></tr>")
    failed = h.get("failed") or []
    failed_line = (f"<div style='margin-top:8px;color:#e05a5a'>⚠ failing: {', '.join(failed)}</div>"
                   if failed else "")
    return f"""
    <div style="background:#262a31; border-radius:8px; padding:14px 18px;
                border-left:6px solid {color}; margin-bottom:18px;">
      <div style="display:flex; align-items:baseline; gap:14px;">
        <h2 style="margin:0; font-size:18px;">Service health</h2>
        <span style="color:{color}; font-weight:700; letter-spacing:.5px;">{label}</span>
      </div>
      <table style="margin-top:10px; width:100%; border-collapse:collapse; font-size:13px;">
        {rows}
      </table>
      {failed_line}
    </div>"""


def render_html(s: dict) -> str:
    base = s["baseline"]
    wd = s["wodehouse"]
    ner = s["ner_filter"]
    raw = s["raw_text"]
    disk = s["disk_data"]

    def card(title, rows, accent="#4a90e2"):
        body = "".join(
            f"<tr><td>{k}</td><td class=v>{v}</td></tr>" for k, v in rows
        )
        return f"""
        <div class=card style="border-top:4px solid {accent}">
          <h2>{title}</h2>
          <table>{body}</table>
        </div>"""

    baseline_card = card("SPGC Baseline (en)", [
        ("Books matched",    human_int(base.get("books_matched"))),
        ("Books aggregated", human_int(base.get("books_aggregated"))),
        ("Missing counts",   human_int(base.get("books_missing_counts"))),
        ("Total tokens",     human_int(base.get("total_tokens"))),
        ("Vocabulary size",  human_int(base.get("vocab_size"))),
    ], accent="#4a90e2")

    wd_card = card("Wodehouse author", [
        ("Books matched",    human_int(wd.get("books_matched"))),
        ("Books aggregated", human_int(wd.get("books_aggregated"))),
        ("Author tokens",    human_int(wd.get("author_tokens"))),
        ("Author vocab",     human_int(wd.get("author_vocab"))),
        ("Affinity rows",    human_int(wd.get("rows_written"))),
    ], accent="#7ed321")

    ner_card = card("NER filter", [
        ("Entities found",      human_int(ner.get("entities_found"))),
        ("Unique surfaces",     human_int(ner.get("unique_surface_forms"))),
        ("Affinity rows",       human_int(ner.get("affinity_rows"))),
        ("Proper nouns dropped",human_int(ner.get("proper_noun_rows_dropped"))),
        ("Clean rows",          human_int(ner.get("clean_rows"))),
    ], accent="#f5a623")

    pending = max(0, raw["source_rsync_mirror"] - raw["books"])
    raw_card = card("Raw text on disk (unified)", [
        ("Unique books (hard-linked)",  f"{human_int(raw['books'])} ({human_bytes(raw['bytes'])})"),
        ("From per-author downloads",   human_int(raw["source_per_author"])),
        ("rsync mirror files (raw, incl. dupes)", human_int(raw["source_rsync_mirror"])),
        ("rsync arrivals not yet linked", human_int(pending)),
        ("Rsync still running",         "yes" if raw["rsync_running"] else "no"),
    ], accent="#bd10e0")

    ollama = s["ollama"]
    loaded_lines = []
    for m in ollama.get("models_loaded", []):
        loaded_lines.append(f"{m['name']} ({m['size_vram_mb']} MB VRAM)")
    ollama_card = card("Ollama LLM", [
        ("API up",         "yes" if ollama.get("up") else "<span style='color:#e05a5a'>NO</span>"),
        ("GPU in container", "yes" if ollama.get("gpu") else "<span style='color:#e05a5a'>NO — fallback to CPU</span>"),
        ("Loaded models",  "<br>".join(loaded_lines) if loaded_lines else "<span style='color:#888'>none</span>"),
        ("Host VRAM",      f"{ollama.get('host_vram_used_mb', 0)} / {ollama.get('host_vram_total_mb', 0)} MB ({ollama.get('host_gpu_util_pct', 0)} % util)"),
    ], accent=("#7ed321" if (ollama.get("gpu") and ollama.get("up")) else "#e05a5a"))

    chroma = s["chroma"]
    chroma_card = card("ChromaDB semantic index", [
        ("DB size on disk", human_bytes(chroma["db_bytes"])),
        ("Build process",   "running" if chroma["build_running"] else "idle"),
        ("Last log line",   f"<code style='font-size:11px'>{chroma['build_last_line'] or '—'}</code>"),
    ], accent="#50e3c2")

    sys_card = card("System", [
        ("Disk /data",  f"{human_bytes(disk['used'])} / {human_bytes(disk['total'])} ({disk['pct_used']} %)"),
        ("Disk free",   human_bytes(disk["free"])),
        ("Host uptime", s["host"]["uptime"]),
        ("Contexts written", human_int(s["contexts_files"])),
    ], accent="#9013fe")

    # authors_analyzed and top_authors stay in /api/status JSON for tooling, but
    # the dashboard no longer renders the two big tables that used to sit below
    # the cards.
    return f"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<meta http-equiv=refresh content=30>
<title>wordcracker status</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; background:#1c1f24; color:#eaeaea; margin:0; padding:24px; }}
  h1 {{ margin:0 0 6px 0; }}
  .subtitle {{ color:#888; font-size:14px; margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }}
  .card {{ background:#262a31; border-radius:8px; padding:14px 18px; }}
  .card h2 {{ margin:2px 0 10px 0; font-size:15px; font-weight:600; color:#fff; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  td {{ padding:3px 0; border-bottom:1px solid #2f343c; }}
  td.v {{ text-align:right; font-variant-numeric:tabular-nums; color:#fff; }}
  td.v small {{ color:#888; }}
  .authors {{ margin-top:20px; background:#262a31; border-radius:8px; padding:14px 18px; }}
  .authors h2 {{ margin:2px 0 10px 0; font-size:15px; font-weight:600; }}
  .authors th {{ text-align:left; color:#888; font-weight:normal; padding:3px 6px 6px 0; font-size:12px; }}
  .authors td {{ padding:4px 6px 4px 0; }}
  footer {{ margin-top:24px; color:#666; font-size:12px; }}
  a {{ color:#7ed321; }}
</style>
</head>
<body>
  <h1>wordcracker · status</h1>
  <div class=subtitle>updated {s['now']} · auto-refresh 30 s · <a href="/api/status">JSON</a></div>
  {_health_card_html(s.get('health', {}))}
  <div class=grid>
    {baseline_card}
    {wd_card}
    {ner_card}
    {raw_card}
    {chroma_card}
    {ollama_card}
    {sys_card}
  </div>

  <footer>Source: <code>~/wordcracker/scripts/status_server.py</code> · Server-on-Wheels · stdlib only</footer>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return  # silence default access log

    def do_GET(self):
        if self.path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        try:
            s = collect_status()
        except Exception as e:
            self._send(500, f"collect_status error: {e}".encode(), "text/plain")
            return
        if self.path.startswith("/api/status"):
            self._send(200, json.dumps(s, indent=2, default=str).encode(), "application/json")
        else:
            self._send(200, render_html(s).encode("utf-8"), "text/html; charset=utf-8")

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8889)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"wordcracker status on http://{args.host}:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
