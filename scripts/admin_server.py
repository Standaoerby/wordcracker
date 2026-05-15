#!/usr/bin/env python3
"""
admin_server.py — minimal upload UI for adding books into the corpus.

Web endpoint that accepts a .zip / .tar.gz / .tar.bz2 / single .txt, extracts
it, hard-links recognized Project Gutenberg book files into /data/raw_text/,
and optionally triggers an incremental ChromaDB reindex.

Routes:
  GET  /              — HTML upload form + recent jobs
  POST /upload        — multipart, parses + links + (optional) triggers reindex
  GET  /status        — JSON: recent jobs, reindex running?, raw_text count
  GET  /health        — "ok"
  GET  /api/status    — alias for /status

Runs inside gutenberg-lab container on :8891. No deps beyond Python 3 stdlib.

Filename → PG id mapping:
  pg(\\d+)\\.txt           → PG{id}.txt   (gutenberg.org/cache/epub style)
  (\\d+)-0\\.txt           → PG{id}.txt   (rsync mirror UTF-8)
  (\\d+)\\.txt             → PG{id}.txt   (rare bare numeric)
  anything else            → skipped, listed in job report
"""
import argparse
import cgi
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAW_DIR     = Path("/workspace/raw_text")
UPLOAD_ROOT = Path("/workspace/uploads")
INDEX_LOCK  = Path("/workspace/spgc/derived/.reindex.lock")
INDEX_LOG   = Path("/workspace/spgc/derived/admin_reindex.log")
JOBS_LOG    = Path("/workspace/spgc/derived/admin_jobs.json")
USER_META   = Path("/workspace/spgc/derived/user_uploads_metadata.csv")
SCRIPTS_DIR = Path("/workspace/scripts")
SPGC_META   = Path("/workspace/spgc/SPGC-metadata-2018-07-18.csv")

# SPGC metadata column order — we mirror it exactly in user_uploads CSV so a
# simple pd.concat in rag_tools._metadata_df works without schema gymnastics.
USER_META_COLS = [
    "id", "title", "author", "authoryearofbirth", "authoryearofdeath",
    "language", "downloads", "subjects", "type",
    "source_filename", "uploaded_ts",
]

PG_PATTERNS = [
    re.compile(r"^pg(\d+)\.txt$",      re.IGNORECASE),
    re.compile(r"^(\d+)-0\.txt$"),
    re.compile(r"^(\d+)\.txt$"),
]
PG_EPUB_PATTERNS = [
    re.compile(r"^pg(\d+)(?:-images)?\.epub$", re.IGNORECASE),
    re.compile(r"^(\d+)\.epub$"),
    re.compile(r"^(\d+)-0\.epub$"),
]

MAX_UPLOAD_MB = 4096   # 4 GB
ALLOWED_EXTS  = {".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".txt", ".epub"}


def _filename_to_pgid(name: str) -> str | None:
    base = os.path.basename(name)
    for p in PG_PATTERNS:
        m = p.match(base)
        if m:
            return f"PG{int(m.group(1))}"
    return None


def _epub_filename_to_pgid(name: str) -> str | None:
    base = os.path.basename(name)
    for p in PG_EPUB_PATTERNS:
        m = p.match(base)
        if m:
            return f"PG{int(m.group(1))}"
    return None


def _epub_metadata_pgid(epub_path: Path) -> str | None:
    """Try to extract PG id from DC:identifier metadata of an EPUB.

    Only matches when the identifier explicitly references Gutenberg (URL,
    urn:gutenberg, or 'gutenberg' in the scheme). Bare integers in DC:identifier
    are commonly ISBN/Goodreads ids for non-PG epubs — we must NOT mistake those
    for PG ids, or PG1234 would collide with random uploads.
    """
    try:
        from ebooklib import epub as _epub
        book = _epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        for _tag, val, _attrs in book.get_metadata("DC", "identifier") or []:
            s = str(val).lower()
            if "gutenberg" not in s:
                continue
            for piece in re.findall(r"\d+", s):
                if 1 <= int(piece) < 1_000_000:
                    return f"PG{int(piece)}"
        return None
    except Exception:
        return None


def _normalize_author(raw: str) -> str:
    """Best-effort 'First Last' → 'Last, First' to align with SPGC schema.

    Already-commaed input is returned as-is. Single token kept as-is. Multi-word
    surnames (van der X) are not handled — fine for a best-effort default; user
    can edit the CSV by hand if it matters.
    """
    raw = raw.strip()
    if not raw or "," in raw:
        return raw
    parts = raw.split()
    if len(parts) < 2:
        return raw
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _extract_epub_metadata(epub_path: Path) -> dict:
    """Read DC metadata (title/creator/language/date/subjects) from an EPUB.

    Returns a dict with keys aligned to SPGC schema columns. Missing fields
    are returned as empty strings / None (callers must tolerate)."""
    out = {
        "title": "", "author": "", "language": "['en']",
        "authoryearofbirth": "", "authoryearofdeath": "",
        "subjects": "", "downloads": 0, "type": "Text",
    }
    try:
        from ebooklib import epub as _epub
        book = _epub.read_epub(str(epub_path), options={"ignore_ncx": True})
        if (t := book.get_metadata("DC", "title")):
            out["title"] = str(t[0][0]).strip()
        creators = book.get_metadata("DC", "creator") or []
        if creators:
            out["author"] = _normalize_author(str(creators[0][0]))
        langs = book.get_metadata("DC", "language") or []
        if langs:
            # SPGC stores Python-list-literal-like strings: "['en']"
            primary = str(langs[0][0]).strip().lower().split("-")[0]  # 'en-US' → 'en'
            if primary:
                out["language"] = f"['{primary}']"
        subjects = book.get_metadata("DC", "subject") or []
        if subjects:
            out["subjects"] = "; ".join(str(s[0]).strip() for s in subjects if s and s[0])
        # date is YYYY or YYYY-MM-DD: pull the year as a weak signal
        dates = book.get_metadata("DC", "date") or []
        if dates:
            m = re.search(r"(\d{4})", str(dates[0][0]))
            if m:
                out["pub_year"] = int(m.group(1))
    except Exception:
        pass
    return out


def _read_user_meta() -> "list[dict]":
    if not USER_META.exists():
        return []
    import csv as _csv
    with open(USER_META, encoding="utf-8", newline="") as fh:
        return list(_csv.DictReader(fh))


def _next_user_id() -> str:
    """Return the next U<N> id. Counts from max existing U id + 1."""
    rows = _read_user_meta()
    max_n = 0
    for r in rows:
        m = re.match(r"^U(\d+)$", r.get("id", ""))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"U{max_n + 1}"


def _append_user_meta(row: dict) -> None:
    """Append a row to user_uploads_metadata.csv, creating header if missing."""
    import csv as _csv
    USER_META.parent.mkdir(parents=True, exist_ok=True)
    new = not USER_META.exists()
    full = {k: row.get(k, "") for k in USER_META_COLS}
    with open(USER_META, "a", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=USER_META_COLS)
        if new:
            w.writeheader()
        w.writerow(full)


def _epub_to_text(epub_path: Path) -> str:
    """EPUB → plain text via ebooklib + BeautifulSoup. Strips html, joins documents."""
    from ebooklib import epub as _epub, ITEM_DOCUMENT
    from bs4 import BeautifulSoup
    book = _epub.read_epub(str(epub_path), options={"ignore_ncx": True})
    pieces = []
    for item in book.get_items():
        if item.get_type() == ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), "html.parser")
            pieces.append(soup.get_text("\n"))
    raw = "\n".join(pieces)
    # collapse blank-line runs, strip lines
    lines = [ln.strip() for ln in raw.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _extract_archive(archive: Path, dest: Path) -> list[Path]:
    """Extract archive into dest, return list of files extracted."""
    out_files: list[Path] = []
    name_lower = archive.name.lower()

    if name_lower.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif name_lower.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(dest)
    elif name_lower.endswith((".tar.bz2", ".tbz2")):
        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(dest)
    elif name_lower.endswith(".tar"):
        with tarfile.open(archive, "r:") as tf:
            tf.extractall(dest)
    elif name_lower.endswith(".txt"):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / archive.name
        shutil.copy(archive, target)
        out_files.append(target)
        return out_files
    elif name_lower.endswith(".epub"):
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / archive.name
        shutil.copy(archive, target)
        out_files.append(target)
        return out_files
    else:
        raise ValueError(f"unsupported archive type: {archive.name}")

    for root, _dirs, files in os.walk(dest):
        for f in files:
            out_files.append(Path(root) / f)
    return out_files


def _link_into_raw(files: list[Path]) -> dict:
    """Hard-link recognized files into /workspace/raw_text. EPUBs are converted
    to text first. EPUBs without a Gutenberg id get a synthetic U<N> id and
    have their DC metadata recorded in user_uploads_metadata.csv."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    added = skipped_existing = skipped_format = epub_converted = epub_failed = 0
    user_uploaded = 0
    new_user_ids: list[str] = []
    sample_skipped: list[str] = []
    for f in files:
        name_lower = f.name.lower()
        # ----- EPUB handling -----
        if name_lower.endswith(".epub"):
            pgid = _epub_filename_to_pgid(f.name) or _epub_metadata_pgid(f)
            is_user_upload = pgid is None
            if is_user_upload:
                pgid = _next_user_id()
            target = RAW_DIR / f"{pgid.lower()}.txt"
            if target.exists():
                skipped_existing += 1
                continue
            try:
                text = _epub_to_text(f)
            except Exception as e:
                epub_failed += 1
                if len(sample_skipped) < 8:
                    sample_skipped.append(f"{f.name} (epub read failed: {str(e)[:60]})")
                continue
            if not text.strip():
                epub_failed += 1
                if len(sample_skipped) < 8:
                    sample_skipped.append(f.name + " (empty after extraction)")
                continue
            target.write_text(text, encoding="utf-8")
            if is_user_upload:
                meta = _extract_epub_metadata(f)
                meta.update({
                    "id": pgid,
                    "source_filename": f.name,
                    "uploaded_ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                if not meta.get("title"):
                    meta["title"] = Path(f.name).stem.replace("_", " ").replace("-", " ").strip()
                if not meta.get("author"):
                    meta["author"] = "Unknown, "
                _append_user_meta(meta)
                user_uploaded += 1
                new_user_ids.append(pgid)
            epub_converted += 1
            added += 1
            continue
        # ----- plain text handling -----
        pgid = _filename_to_pgid(f.name)
        if not pgid:
            skipped_format += 1
            if len(sample_skipped) < 8:
                sample_skipped.append(f.name)
            continue
        target = RAW_DIR / f"{pgid.lower()}.txt"
        if target.exists():
            skipped_existing += 1
            continue
        try:
            os.link(f, target)
        except OSError:
            shutil.copy(f, target)
        added += 1
    return {
        "added":           added,
        "skipped_existing": skipped_existing,
        "skipped_format":   skipped_format,
        "epub_converted":   epub_converted,
        "epub_failed":      epub_failed,
        "user_uploaded":    user_uploaded,
        "new_user_ids":     new_user_ids,
        "sample_skipped":   sample_skipped,
    }


def _read_jobs() -> list[dict]:
    if JOBS_LOG.exists():
        try:
            return json.loads(JOBS_LOG.read_text())
        except Exception:
            return []
    return []


def _append_job(job: dict) -> None:
    jobs = _read_jobs()
    jobs.insert(0, job)
    jobs = jobs[:50]
    JOBS_LOG.parent.mkdir(parents=True, exist_ok=True)
    JOBS_LOG.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))


def _reindex_running() -> bool:
    """Either lock file present, or any build_index_raw process active."""
    if INDEX_LOCK.exists():
        # stale lock detection: empty file older than 30 min → ignore
        try:
            if time.time() - INDEX_LOCK.stat().st_mtime > 1800 and INDEX_LOCK.stat().st_size == 0:
                INDEX_LOCK.unlink()
            else:
                return True
        except OSError:
            pass
    try:
        proc = subprocess.run(["pgrep", "-f", "build_index_raw"], capture_output=True, text=True)
        return bool(proc.stdout.strip())
    except Exception:
        return False


def _trigger_reindex_async() -> dict:
    """Start build_index_raw in background. Returns info dict."""
    if _reindex_running():
        return {"started": False, "reason": "reindex already running"}
    INDEX_LOCK.parent.mkdir(parents=True, exist_ok=True)
    INDEX_LOCK.write_text(f"{time.time()}")

    cmd = [
        "python", "-u", str(SCRIPTS_DIR / "build_index_raw.py"),
        "--raw-dir",   str(RAW_DIR),
        "--metadata",  str(SPGC_META),
        "--db-path",   "/workspace/chroma_db",
        "--collection", "gutenberg-index",
        "--batch", "512",
    ]
    log_fh = open(INDEX_LOG, "w")

    def _runner():
        try:
            subprocess.run(cmd, stdout=log_fh, stderr=subprocess.STDOUT)
        finally:
            log_fh.close()
            try:
                INDEX_LOCK.unlink()
            except OSError:
                pass

    threading.Thread(target=_runner, daemon=True).start()
    return {"started": True, "cmd": " ".join(cmd), "log": str(INDEX_LOG)}


PAGE = r"""<!doctype html>
<html lang=ru>
<head>
<meta charset=utf-8>
<title>wordcracker · admin</title>
<style>
  body { font-family: ui-sans-serif, system-ui, sans-serif; background:#1c1f24; color:#eaeaea;
         max-width:880px; margin:32px auto; padding:0 20px; }
  h1 { margin:0 0 6px 0; }
  .meta { color:#888; font-size:13px; margin-bottom:24px; }
  form { background:#262a31; border-radius:8px; padding:18px 22px; border-left:3px solid #50e3c2; }
  input[type=file] { background:#1a1d22; color:#eaeaea; padding:8px; border-radius:4px;
                     border:1px solid #2f343c; width:100%; }
  label { display:block; margin-top:12px; font-size:13px; color:#aaa; }
  label.inline { display:inline; margin-left:8px; }
  button { background:#50e3c2; border:0; color:#0a0c10; font-weight:600; padding:10px 24px;
           border-radius:4px; cursor:pointer; margin-top:16px; font-size:14px; }
  button:disabled { opacity:.5; cursor:wait; }
  .grid { display:grid; grid-template-columns:repeat(2,1fr); gap:8px 16px; margin-top:18px; }
  .pill { background:#1a1d22; padding:8px 12px; border-radius:6px; font-variant-numeric:tabular-nums; }
  .pill b { color:#7ed321; }
  .jobs { margin-top:28px; }
  .job { background:#262a31; border-radius:6px; padding:10px 14px; margin-bottom:8px;
         border-left:3px solid #888; font-size:13px; font-family:ui-monospace,monospace; }
  .job.ok    { border-color:#7ed321; }
  .job.fail  { border-color:#e05a5a; }
  .progress { display:none; margin-top:14px; color:#888; }
  .err { color:#e05a5a; }
  code { background:#1a1d22; padding:1px 5px; border-radius:3px; }
  a { color:#7ed321; }
</style>
</head>
<body>
<h1>wordcracker · admin</h1>
<div class=meta>Загрузка архива книг → extract → hard-link в /data/raw_text → reindex.</div>

<form id=f enctype=multipart/form-data>
  <input type=file name=archive id=archive required
         accept=".zip,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.txt,.epub">
  <label><input type=checkbox name=reindex checked> запустить reindex после распаковки</label>
  <label class=meta>Принимаются: <code>.zip</code> <code>.tar.gz</code> <code>.tar.bz2</code> <code>.txt</code> <code>.epub</code>.
    Распознаются имена <code>pgN.txt</code>, <code>N-0.txt</code>, <code>N.txt</code>, <code>pgN.epub</code>, <code>N.epub</code> (N = PG id) — Gutenberg-формат.
    Любой <b>другой EPUB</b> получает <code>U&lt;N&gt;</code> id и метадата (title/author/language/subjects) тянется из DC-tags EPUB.<br>
    Конвертация через ebooklib + BeautifulSoup. <code>.txt</code> без распознанного PG id попадает в <code>skipped_format</code> — для своих текстов оборачивайте в EPUB.<br>
    <b style="color:#7ed321">Cloudflare Pro лимит — 200 MB на запрос.</b> Крупнее — <code>scp ... claude@192.168.68.54:/data/uploads_manual/</code> + ручной pickup.</label>
  <button id=send>Загрузить</button>
  <div id=prog class=progress></div>
</form>

<div class=jobs id=jobs></div>

<script>
const f = document.getElementById('f');
const prog = document.getElementById('prog');
const send = document.getElementById('send');
const jobs = document.getElementById('jobs');

async function refreshJobs() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    jobs.innerHTML = '<div class=meta>Текущее: <code>/data/raw_text</code> = <b>'+d.raw_text_count+'</b> книг ('+d.raw_text_pg+' PG + <b style="color:#50e3c2">'+d.raw_text_user+'</b> user) · reindex: '+(d.reindex_running?'<b style="color:#7ed321">running</b>':'idle')+'</div>';
    for (const j of d.jobs) {
      const cls = j.error ? 'fail' : 'ok';
      const userTag = j.user_uploaded ? ' · <span style="color:#50e3c2">U+'+j.user_uploaded+'</span>' : '';
      const status = j.error ? '❌ '+j.error : '✅ added='+j.added+' skipped='+(j.skipped_existing+j.skipped_format)+userTag;
      jobs.innerHTML += `<div class="job ${cls}">${j.ts} · ${j.filename} (${(j.bytes/1024/1024).toFixed(1)} MB) · ${status}${j.reindex_triggered?' · reindex triggered':''}</div>`;
    }
  } catch(e) { jobs.innerHTML = '<div class="err">jobs load failed: '+e.message+'</div>'; }
}

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  const file = document.getElementById('archive').files[0];
  if (!file) return;
  send.disabled = true;
  prog.style.display = 'block';
  prog.textContent = 'uploading ' + (file.size/1024/1024).toFixed(1) + ' MB…';
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload');
  xhr.upload.onprogress = e => {
    if (e.lengthComputable) {
      const p = (e.loaded * 100 / e.total).toFixed(0);
      prog.textContent = 'uploading ' + p + '%';
    }
  };
  xhr.onload = () => {
    send.disabled = false;
    try {
      const r = JSON.parse(xhr.responseText);
      prog.textContent = r.error
        ? 'ERROR: ' + r.error
        : `done: +${r.added} added, ${r.skipped_existing+r.skipped_format} skipped`;
      refreshJobs();
    } catch(err) {
      prog.textContent = 'bad response: ' + xhr.responseText.slice(0,200);
    }
  };
  xhr.onerror = () => { send.disabled = false; prog.textContent = 'network error'; };
  const fd = new FormData(f);
  xhr.send(fd);
});

refreshJobs();
setInterval(refreshJobs, 8000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k): return

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        if self.path in ("/status", "/api/status"):
            pg_count = sum(1 for _ in RAW_DIR.glob("pg*.txt")) if RAW_DIR.exists() else 0
            user_count = sum(1 for _ in RAW_DIR.glob("u*.txt")) if RAW_DIR.exists() else 0
            return self._json(200, {
                "raw_text_count": pg_count + user_count,
                "raw_text_pg":    pg_count,
                "raw_text_user":  user_count,
                "reindex_running": _reindex_running(),
                "jobs": _read_jobs(),
            })
        return self._html(PAGE)

    def do_POST(self):
        if self.path != "/upload":
            return self._json(404, {"error": "not found"})
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._json(400, {"error": "multipart/form-data required"})
        clen = int(self.headers.get("Content-Length", "0"))
        if clen > MAX_UPLOAD_MB * 1024 * 1024:
            return self._json(413, {"error": f"upload >{MAX_UPLOAD_MB} MB"})

        env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(clen)}
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env)
        fileitem = form["archive"] if "archive" in form else None
        # cgi.FieldStorage.__bool__ raises TypeError on Python 3.11 (deprecated module
        # bug), so test against None and use getattr — never `not fileitem`.
        if fileitem is None or not getattr(fileitem, "filename", None):
            return self._json(400, {"error": "missing field 'archive'"})

        # save upload
        ts = time.strftime("%Y%m%d-%H%M%S")
        upload_dir = UPLOAD_ROOT / ts
        upload_dir.mkdir(parents=True, exist_ok=True)
        archive_path = upload_dir / os.path.basename(fileitem.filename)
        with open(archive_path, "wb") as fh:
            shutil.copyfileobj(fileitem.file, fh)

        job: dict = {"ts": ts, "filename": archive_path.name,
                     "bytes": archive_path.stat().st_size}
        try:
            extracted_dir = upload_dir / "extracted"
            extracted_dir.mkdir(exist_ok=True)
            files = _extract_archive(archive_path, extracted_dir)
            stats = _link_into_raw(files)
            job.update(stats)
            job["extracted_count"] = len(files)
        except Exception as e:
            job["error"] = f"extract/link failed: {e}"
            _append_job(job)
            return self._json(500, job)

        # cleanup extracted dir (hard-links keep the data alive in /data/raw_text)
        shutil.rmtree(extracted_dir, ignore_errors=True)

        # optional reindex
        do_reindex = "reindex" in form and form.getvalue("reindex")
        if do_reindex and job.get("added", 0) > 0:
            r = _trigger_reindex_async()
            job["reindex_triggered"] = r.get("started", False)
            job["reindex_info"]      = r
        else:
            job["reindex_triggered"] = False

        _append_job(job)
        return self._json(200, job)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8891)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"wordcracker admin on http://{args.host}:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
