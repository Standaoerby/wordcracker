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
import json
import os
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DERIVED = Path("/data/spgc/derived")
TOKENS_DIR = Path("/data/spgc/SPGC-tokens-2018-07-18")
COUNTS_DIR = Path("/data/spgc/SPGC-counts-2018-07-18")
METADATA_CSV = Path("/data/spgc/SPGC-metadata-2018-07-18.csv")
WODEHOUSE_RAW = Path("/data/wodehouse_raw")
GUTENBERG_RAW = Path("/data/gutenberg_raw")


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


def collect_status():
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

    gb_raw_files = count_files(GUTENBERG_RAW, "**/*-0.txt") if GUTENBERG_RAW.exists() else 0
    gb_raw_bytes = dir_size_bytes(GUTENBERG_RAW) if GUTENBERG_RAW.exists() else 0

    wd_raw_files = count_files(WODEHOUSE_RAW, "*.txt")
    wd_raw_bytes = dir_size_bytes(WODEHOUSE_RAW)

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

    return {
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
            "wodehouse_files": wd_raw_files,
            "wodehouse_bytes": wd_raw_bytes,
            "gutenberg_mirror_files": gb_raw_files,
            "gutenberg_mirror_bytes": gb_raw_bytes,
            "rsync_running": rsync_alive,
        },
        "contexts_files": contexts,
        "disk_data": {
            "total": du.total,
            "used":  du.used,
            "free":  du.free,
            "pct_used": round(du.used / du.total * 100, 1),
        },
        "host": {
            "uptime": uptime,
        },
    }


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

    raw_card = card("Raw text on disk", [
        ("Wodehouse books (per-author DL)", f"{raw['wodehouse_files']} ({human_bytes(raw['wodehouse_bytes'])})"),
        ("Gutenberg mirror (rsync)",        f"{raw['gutenberg_mirror_files']} ({human_bytes(raw['gutenberg_mirror_bytes'])})"),
        ("Rsync still running",             "yes" if raw["rsync_running"] else "no"),
    ], accent="#bd10e0")

    sys_card = card("System", [
        ("Disk /data",  f"{human_bytes(disk['used'])} / {human_bytes(disk['total'])} ({disk['pct_used']} %)"),
        ("Disk free",   human_bytes(disk["free"])),
        ("Host uptime", s["host"]["uptime"]),
        ("Contexts written", human_int(s["contexts_files"])),
    ], accent="#9013fe")

    auth_rows = ""
    for a in s["authors_analyzed"]:
        auth_rows += (
            f"<tr><td>{a['slug']}</td>"
            f"<td class=v>{human_int(a.get('books_matched'))}</td>"
            f"<td class=v>{human_int(a.get('books_aggregated'))}</td>"
            f"<td class=v>{human_int(a.get('author_tokens'))}</td>"
            f"<td class=v>{human_int(a['rows'])}</td>"
            f"<td class=v>{human_bytes(a['size'])}</td></tr>"
        )
    if not auth_rows:
        auth_rows = "<tr><td colspan=6 style='text-align:center;color:#888'>none yet</td></tr>"

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
  <div class=grid>
    {baseline_card}
    {wd_card}
    {ner_card}
    {raw_card}
    {sys_card}
  </div>

  <div class=authors>
    <h2>Authors analyzed (affinity CSVs in /data/spgc/derived/)</h2>
    <table>
      <tr><th>slug</th><th style='text-align:right'>books matched</th><th style='text-align:right'>aggregated</th><th style='text-align:right'>author tokens</th><th style='text-align:right'>affinity rows</th><th style='text-align:right'>file size</th></tr>
      {auth_rows}
    </table>
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
