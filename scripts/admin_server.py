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
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# v2.10.1: admin shares the gutenberg-lab container's python process with
# chat_server, but unlike chat_server it didn't add the repo root to
# sys.path. So `from scripts.v2.observability import recent_failures` in
# the /api/failed handler was raising ModuleNotFoundError → 500. Add the
# same two entries chat_server uses so `scripts.v2.*` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def _open_library_enrich(meta: dict, timeout: float = 8.0) -> dict:
    """Fill missing metadata fields from Open Library if the EPUB DC tags are
    incomplete. Best-effort: any failure (network, rate-limit, no match)
    leaves the original meta dict unchanged.

    Triggered only when something is actually missing — we don't overwrite
    fields the EPUB already provided.

    https://openlibrary.org/dev/docs/api/search
    """
    title = (meta.get("title") or "").strip()
    if not title:
        return meta
    has_author = bool((meta.get("author") or "").strip()) and meta["author"] != "Unknown, "
    has_year   = bool(meta.get("pub_year"))
    has_subj   = bool((meta.get("subjects") or "").strip())
    if has_author and has_year and has_subj:
        return meta

    try:
        import urllib.parse, urllib.request, json as _json
        params = {"title": title, "limit": 3}
        # narrow search if we already know the author — better match precision
        if has_author:
            params["author"] = meta["author"].split(",")[0]
        q = urllib.parse.urlencode(params)
        url = f"https://openlibrary.org/search.json?{q}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "wordcracker/1.0 (NAS-uploader; contact via slovoeb.net)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = _json.load(r)
        docs = d.get("docs", [])
        if not docs:
            meta["_ol_lookup"] = "no match"
            return meta
        top = docs[0]
        if not has_author and top.get("author_name"):
            meta["author"] = _normalize_author(top["author_name"][0])
        if not has_year:
            y = top.get("first_publish_year")
            if isinstance(y, int) and 1500 <= y <= 2030:
                meta["pub_year"] = y
                # SPGC schema uses authoryearofbirth (writing prime ≈ birth+30)
                # for time-bucketing. We have publication year, so derive a
                # birth-year proxy: pub_year - 30. Lets the U-book show up in
                # word_freq_timeline at roughly the right bucket.
                if not meta.get("authoryearofbirth"):
                    meta["authoryearofbirth"] = y - 30
        if not has_subj and top.get("subject"):
            meta["subjects"] = "; ".join(s for s in top["subject"][:8] if s)
        meta["_ol_lookup"] = top.get("key", "matched")
    except Exception as e:
        meta["_ol_lookup"] = f"error: {type(e).__name__}"
    return meta


def _normalize_epub_lang(raw: str) -> str:
    """EPUB <dc:language> → ISO-639-1 (2-letter), or '' if unknown/absent.

    'en' / 'en-US' / 'en_us' → 'en'; ISO-639-2/B 'eng' → 'en'. Unknown
    3-letter codes (no 639-2/B mapping) and garbage → '' — rejected, not
    guessed, so a bad/absent tag is flagged unknown downstream instead of
    silently mislabeling the book English. Reuses
    rag_tools._ISO_639_2_TO_1 as the single ISO map (TZ S-T2 Group B/C).
    """
    try:
        from scripts.lang_norm import normalize_lang
    except ImportError:  # pragma: no cover — bare-name fallback
        from lang_norm import normalize_lang
    return normalize_lang(raw)


def _extract_epub_metadata(epub_path: Path) -> dict:
    """Read DC metadata (title/creator/language/date/subjects) from an EPUB.

    Returns a dict with keys aligned to SPGC schema columns. Missing fields
    are returned as empty strings / None (callers must tolerate)."""
    out = {
        "title": "", "author": "", "language": "",
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
            # SPGC stores Python-list-literal-like strings: "['en']". Map to
            # ISO-639-1 and reject unknowns (eng->en, en-US->en, xx->'') so we
            # never persist a 3-letter ['eng'] (the corruption Group B had to
            # defend against). No DC tag → language stays "" (set above),
            # NOT defaulted to ['en'] — downstream normalize flags it unknown.
            code = _normalize_epub_lang(str(langs[0][0]))
            out["language"] = f"['{code}']" if code else ""
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
                # Try Open Library before falling back to "Unknown" — fills
                # missing author/year/subjects from the public catalog when
                # the EPUB itself didn't ship DC metadata.
                meta = _open_library_enrich(meta)
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

    # Tokenize newly-added user books into SPGC-compatible format so
    # top_ngrams_by_author / affinity_by_author / word_contexts /
    # word_collocates / lexical_diversity start working for them
    # immediately (without a full reindex round trip).
    tokenize_summary = []
    for u_id in new_user_ids:
        r = _tokenize_user_book(u_id)
        tokenize_summary.append(r)

    return {
        "added":           added,
        "skipped_existing": skipped_existing,
        "skipped_format":   skipped_format,
        "epub_converted":   epub_converted,
        "epub_failed":      epub_failed,
        "user_uploaded":    user_uploaded,
        "new_user_ids":     new_user_ids,
        "tokenize":        tokenize_summary,
        "sample_skipped":   sample_skipped,
    }


def _tokenize_user_book(book_id: str) -> dict:
    """Run scripts/tokenize_user_books.py for a single freshly-uploaded U<N>.
    Synchronous — these are ~1s on CPU for typical novel-sized EPUBs."""
    try:
        proc = subprocess.run(
            ["python", "-u", str(SCRIPTS_DIR / "tokenize_user_books.py"),
             "--book", book_id],
            capture_output=True, text=True, timeout=60,
        )
        ok = proc.returncode == 0
        return {"id": book_id, "ok": ok,
                "stdout": proc.stdout.strip()[-200:],
                "stderr": proc.stderr.strip()[-200:] if not ok else ""}
    except Exception as e:
        return {"id": book_id, "ok": False, "error": str(e)[:120]}


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


def _admin_nav_html() -> str:
    """Inserted into PAGE so admin upload page links to failed-query view
    and the library."""
    return ('<div class=meta style="margin-top:-12px; margin-bottom:14px;">'
            '<a href="/library/page" style="color:#7ed321;">→ library</a> '
            '· <a href="/failed" style="color:#7ed321;">→ failed queries</a>'
            '· <a href="/feedback" style="color:#e08080;">🚩 flagged answers</a>'
            '</div>')


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
  .preview-block { background:#1f2227; border-left:3px solid #50e3c2; border-radius:6px;
                   padding:12px 16px; font-size:13px; }
  .preview-row { font-family:ui-monospace,monospace; padding:3px 0; color:#bbb; }
  code { background:#1a1d22; padding:1px 5px; border-radius:3px; }
  a { color:#7ed321; }
</style>
</head>
<body>
<h1>wordcracker · admin</h1>
<div class=meta>Загрузка архива книг → extract → hard-link в /data/raw_text → reindex.</div>
<div class=meta style="margin-top:-12px; margin-bottom:14px;">
  <a href="/failed" style="color:#7ed321;">→ failed queries log</a>
</div>

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

<div id=preview style="margin-top:20px"></div>
<div class=jobs id=jobs></div>

<script>
const f = document.getElementById('f');
const prog = document.getElementById('prog');
const send = document.getElementById('send');
const jobs = document.getElementById('jobs');
const preview = document.getElementById('preview');

function renderPreview(r) {
  // Show the newly-added books with whatever metadata Open Library + tokenize
  // produced. Empty if nothing was added.
  if (r.error) { preview.innerHTML = '<div class="job fail">❌ '+r.error+'</div>'; return; }
  if (!r.new_user_ids?.length && !r.added) { preview.innerHTML = ''; return; }
  let html = '<div class="preview-block">';
  html += `<div class=meta>📦 <b>${r.added} added</b> · ${r.epub_converted||0} EPUB→txt · ${r.user_uploaded||0} user (U-prefix) · ${r.skipped_existing||0} skipped`;
  if (r.skipped_format) html += ' · ' + r.skipped_format + ' unrecognized';
  html += '</div>';
  // Per-book row for U-uploads (with tokenize result)
  if (r.tokenize?.length) {
    html += '<div class=meta>🔤 Tokenize:</div>';
    for (const t of r.tokenize) {
      const status = t.ok ? '✓' : '✗';
      const detail = t.ok ? (t.stdout.split('\n').pop() || '') : (t.stderr || t.error || '');
      html += `<div class="preview-row">${status} <code>${t.id}</code> · ${detail}</div>`;
    }
  }
  if (r.reindex_triggered) {
    html += '<div class=meta>🔄 Reindex запущен. Прогресс на <a href="https://status.slovoeb.net">status dashboard</a> (поле <code>reindex_progress</code>) — обычно ~30-60s на 10 новых книг.</div>';
  } else if (r.user_uploaded) {
    html += '<div class=meta>ℹ️ Для семантического поиска по новым книгам нужен reindex (чекбокс выше при следующей загрузке).</div>';
  }
  html += '</div>';
  preview.innerHTML = html;
}

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
      renderPreview(r);
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


# v2.7: standalone HTML for /failed (failed-query log viewer).
_FAILED_PAGE = r"""<!doctype html>
<html lang=ru>
<head>
<meta charset=utf-8>
<title>wordcracker · failed queries</title>
<style>
  body { font-family: ui-sans-serif, system-ui, sans-serif; background:#1c1f24; color:#eaeaea;
         max-width:1100px; margin:32px auto; padding:0 20px; }
  h1 { margin:0 0 6px 0; }
  .meta { color:#888; font-size:13px; margin-bottom:24px; }
  .nav { margin-bottom:16px; }
  .nav a { color:#7ed321; margin-right:14px; }
  .filter { background:#262a31; padding:10px 14px; border-radius:6px; margin-bottom:16px;
            display:flex; gap:14px; align-items:center; }
  .filter label { color:#aaa; font-size:13px; }
  .filter select { background:#1a1d22; color:#eaeaea; border:1px solid #2f343c;
                   padding:4px 8px; border-radius:4px; }
  table { border-collapse:collapse; width:100%; background:#262a31;
          border-radius:6px; overflow:hidden; }
  thead { background:#1a1d22; }
  th { text-align:left; padding:10px 14px; font-size:12px; color:#888;
       text-transform:uppercase; letter-spacing:0.5px; font-weight:normal;
       border-bottom:1px solid #2f343c; }
  td { padding:10px 14px; border-bottom:1px solid #1f2227; vertical-align:top;
       font-size:13px; }
  td.q { color:#a0c4ff; max-width:340px; }
  td.intent { color:#888; font-family:ui-monospace,monospace; font-size:12px;
              white-space:nowrap; }
  td.intent .original { color:#e0a04e; }
  td.kind .clarify { background:#3a3320; color:#e0a04e; padding:2px 7px;
                     border-radius:9px; font-size:11px; }
  td.kind .oos     { background:#2d3a4d; color:#a0c4ff; padding:2px 7px;
                     border-radius:9px; font-size:11px; }
  td.ans { color:#bbb; max-width:380px; font-size:12px; }
  td.reason { color:#888; max-width:240px; font-size:12px; font-style:italic; }
  td.ts { color:#666; font-family:ui-monospace,monospace; font-size:11px;
          white-space:nowrap; }
  .empty { color:#666; text-align:center; padding:40px 0; }
</style>
</head>
<body>
<h1>wordcracker · failed queries</h1>
<div class=meta>Запросы, на которые планировщик ответил <code>clarify</code> или
<code>out_of_scope</code>. Новее — выше. Ring buffer хранит последние ~256
запросов; полная история в JSONL логах.</div>

<div class=nav>
  <a href="/">← upload</a>
  <a href="/feedback" style="color:#e08080;">🚩 flagged answers</a>
  <a href="/api/failed">JSON</a>
</div>

<h3 style="margin-top:24px; margin-bottom:8px; color:#888; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; font-weight:normal;">Top repeated failed phrases (regex-rule candidates)</h3>
<table id=top-phrases style="margin-bottom:24px;">
  <thead>
    <tr>
      <th>count</th>
      <th>phrase</th>
      <th>latest intent</th>
      <th>kinds</th>
    </tr>
  </thead>
  <tbody id=top-body><tr><td colspan=4 class=empty>загрузка…</td></tr></tbody>
</table>

<h3 style="margin-bottom:8px; color:#888; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; font-weight:normal;">Recent fails (newest first)</h3>
<div class=filter>
  <label>kind:</label>
  <select id=kind>
    <option value="">all</option>
    <option value=clarify>clarify</option>
    <option value=out_of_scope>out_of_scope</option>
  </select>
  <span style="flex:1"></span>
  <span class=meta id=count>загрузка…</span>
</div>

<table id=tbl>
  <thead>
    <tr>
      <th>time</th>
      <th>kind</th>
      <th>intent</th>
      <th>query</th>
      <th>answer (truncated)</th>
      <th>reason</th>
    </tr>
  </thead>
  <tbody id=body><tr><td colspan=6 class=empty>загрузка…</td></tr></tbody>
</table>

<script>
let RAW = [];
let TOP = [];
// v2.10: guarded refresh chain.
// The old `setInterval(load, 15000)` created overlapping fetches when
// /api/failed was slow (big ring buffer + lm-sensors subprocess) —
// Stan reported the failed-log page freezing. Three fixes here:
//   1. AbortController cancels the previous request if a new tick fires
//      while it's still in flight (defensive — rarely needed once #2 is
//      in place).
//   2. Re-schedule with setTimeout INSIDE load()'s `.finally` instead
//      of a blind setInterval. New tick can't start until previous
//      one fully resolved.
//   3. Pause polling when the page tab is hidden (visibilityState),
//      resume on focus. Stops background-tab buildup.
let inflightAbort = null;
let pollTimer = null;
let isLoading = false;
const POLL_MS = 15000;

async function load() {
  if (isLoading) return;
  isLoading = true;
  if (inflightAbort) try { inflightAbort.abort(); } catch {}
  inflightAbort = new AbortController();
  try {
    const r = await fetch('/api/failed', {
      signal: inflightAbort.signal, cache: 'no-store',
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    RAW = (d.failed || []).slice(0, 100);
    TOP = (d.top_phrases || []).slice(0, 30);
    renderTop();
    render();
  } catch (e) {
    if (e.name !== 'AbortError') {
      document.getElementById('body').innerHTML =
        '<tr><td colspan=6 class=empty>ошибка загрузки: ' +
        (e.message || e.name) + '</td></tr>';
    }
  } finally {
    isLoading = false;
    schedule();
  }
}
function schedule() {
  if (pollTimer) clearTimeout(pollTimer);
  if (document.visibilityState === 'hidden') return;  // pause in bg
  pollTimer = setTimeout(load, POLL_MS);
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') load();
});
function renderTop() {
  if (!TOP.length) {
    document.getElementById('top-body').innerHTML =
      '<tr><td colspan=4 class=empty>пусто</td></tr>';
    return;
  }
  document.getElementById('top-body').innerHTML = TOP.map(r => {
    const kinds = Object.entries(r.kinds || {})
      .map(([k, v]) => k + '×' + v).join(', ');
    return `<tr>
      <td style="font-weight:600; color:#e0a04e;">${r.count}</td>
      <td class=q>${escapeHtml(r.phrase || '')}</td>
      <td class=intent>${escapeHtml(r.latest_intent || '?')}</td>
      <td class=intent>${escapeHtml(kinds)}</td>
    </tr>`;
  }).join('');
}
function render() {
  const kind = document.getElementById('kind').value;
  const rows = kind ? RAW.filter(r => r.failure_kind === kind) : RAW;
  document.getElementById('count').textContent = rows.length + ' rows';
  if (!rows.length) {
    document.getElementById('body').innerHTML =
      '<tr><td colspan=6 class=empty>пусто</td></tr>';
    return;
  }
  document.getElementById('body').innerHTML = rows.map(r => {
    const t = (r.ts || '').replace('T', ' ').slice(0, 19);
    const kindCls = r.failure_kind === 'clarify' ? 'clarify' : 'oos';
    const kindTxt = r.failure_kind || '?';
    const intent = r.intent || '?';
    const orig = r.original_intent && r.original_intent !== r.intent
                  ? ` <span class=original>(was: ${escapeHtml(r.original_intent)})</span>`
                  : '';
    const q = escapeHtml(r.question_truncated || '');
    const ans = escapeHtml((r.answer_truncated || '').slice(0, 200));
    const reason = escapeHtml(r.failure_reason || '—');
    return `<tr>
      <td class=ts>${t}</td>
      <td class=kind><span class="${kindCls}">${kindTxt}</span></td>
      <td class=intent>${escapeHtml(intent)}${orig}</td>
      <td class=q>${q}</td>
      <td class=ans>${ans}</td>
      <td class=reason>${reason}</td>
    </tr>`;
  }).join('');
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
document.getElementById('kind').addEventListener('change', render);
load();
</script>
</body>
</html>
"""


def _render_library_page() -> str:
    """Sprint 19 — admin library: list + inline actions per book."""
    return r"""<!doctype html>
<html lang=ru>
<head>
<meta charset=utf-8>
<title>wordcracker · library</title>
<style>
body { font-family: ui-sans-serif, system-ui; max-width: 1200px;
       margin: 24px auto; padding: 0 24px; background:#181a1f;
       color:#eaeaea; }
h1 { font-size: 22px; margin: 0 0 6px 0; }
.meta { color: #888; font-size: 13px; margin-bottom: 18px; }
.meta a { color: #7ed321; }
.summary { background:#1f242b; border:1px solid #2f343c;
           padding:10px 14px; border-radius:6px; margin-bottom:14px;
           font-size:13px; color:#aaa; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { border-bottom: 1px solid #2f343c; padding: 8px 10px;
         text-align: left; vertical-align: top; }
th { background: #1f242b; color: #888; font-weight: normal;
     text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
tr:hover td { background: #1d2128; }
.health-ok { color:#7ed321; }
.health-no_tokens, .health-no_counts { color:#f5a623; }
.health-truncated, .health-no_raw { color:#d0021b; }
.id { font-family: ui-monospace, monospace; color:#a0c4ff; }
.btn { background:#262a31; color:#a0c4ff; border:1px solid #2f343c;
       padding:4px 10px; border-radius:4px; font-size:12px;
       cursor:pointer; margin-right:4px; text-decoration:none;
       display:inline-block; }
.btn:hover { border-color:#a0c4ff; }
.btn-danger { color:#f08080; }
.btn-danger:hover { border-color:#f08080; }
.stats-overlay { display:none; position:fixed; inset:0;
                 background:rgba(0,0,0,0.7); z-index:100;
                 align-items:center; justify-content:center; }
.stats-overlay.show { display:flex; }
.stats-card { background:#1f242b; border-radius:8px;
              padding:20px 24px; max-width:700px; max-height:80vh;
              overflow:auto; }
.stats-card h2 { margin:0 0 12px 0; color:#7ed321; }
.stats-card .close { float:right; cursor:pointer; color:#666;
                     font-size:20px; }
.stats-card .close:hover { color:#eaeaea; }
.stats-card pre { background:#0f1115; padding:8px; border-radius:4px;
                  font-size:12px; max-height:200px; overflow:auto; }
</style>
</head>
<body>
<h1>wordcracker · library</h1>
<div class=meta>
  <a href="/">← upload</a> · <a href="/failed">failed queries</a>
  · <a href="/feedback" style="color:#e08080;">🚩 flagged answers</a>
  · <button class=btn onclick="triggerReindex()" style="margin-left:12px">↻ reindex Chroma</button>
  <span id=reindex-status style="margin-left:8px; color:#888"></span>
</div>
<div class=summary id=summary>загружаем…</div>
<table>
<thead><tr>
<th>id</th><th>title</th><th>author</th><th>lang</th>
<th>size</th><th>health</th><th>uploaded</th><th>actions</th>
</tr></thead>
<tbody id=tbody><tr><td colspan=8>загрузка…</td></tr></tbody>
</table>

<div id=stats-overlay class=stats-overlay onclick="if(event.target.id==='stats-overlay')hideStats()">
  <div class=stats-card id=stats-card></div>
</div>

<script>
function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(1) + ' MB';
}
async function loadLibrary() {
  const r = await fetch('/api/library');
  const data = await r.json();
  const books = data.books || [];
  const counts = {ok:0, broken:0, total:books.length, bytes:0};
  for (const b of books) {
    counts.bytes += b.raw_bytes || 0;
    if (b.health === 'ok') counts.ok++; else counts.broken++;
  }
  document.getElementById('summary').textContent =
    `${counts.total} книг · ${counts.ok} ok · ${counts.broken} проблем · ${fmtBytes(counts.bytes)} raw`;
  const tbody = document.getElementById('tbody');
  if (!books.length) {
    tbody.innerHTML = '<tr><td colspan=8>загруженных книг ещё нет</td></tr>';
    return;
  }
  tbody.innerHTML = books.map(b => `
    <tr>
      <td class=id>${b.id}</td>
      <td>${escapeHtml(b.title) || '—'}</td>
      <td>${escapeHtml(b.author) || '—'}</td>
      <td>${b.language || '—'}</td>
      <td>${fmtBytes(b.raw_bytes)}</td>
      <td class="health-${b.health}">${b.health}</td>
      <td>${b.uploaded_ts || '—'}</td>
      <td>
        <button class=btn onclick="showStats('${b.id}')">stats</button>
        <a class=btn href="/book/${b.id}/raw" download>raw</a>
        <button class=btn onclick="reprocess('${b.id}')">reprocess</button>
        <button class="btn btn-danger" onclick="deleteBook('${b.id}')">delete</button>
      </td>
    </tr>
  `).join('');
}
function escapeHtml(s) {
  if (!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;');
}
async function showStats(id) {
  const r = await fetch(`/book/${id}/stats`);
  const d = await r.json();
  const card = document.getElementById('stats-card');
  if (d.error) {
    card.innerHTML = `<span class=close onclick=hideStats()>×</span>
      <h2>${id}</h2><p>error: ${d.error}</p>`;
  } else {
    const headerWarn = d.has_gutenberg_header
      ? '<p style="color:#f5a623">⚠ Gutenberg header не зачищен</p>' : '';
    const footerWarn = d.has_gutenberg_footer
      ? '<p style="color:#f5a623">⚠ Gutenberg footer не зачищен</p>' : '';
    const top = (d.top_words || []).slice(0, 20)
      .map(x => `${x.word} (${x.count})`).join(', ');
    const samples = (d.sample_paragraphs || [])
      .map(s => `<pre>${escapeHtml(s)}</pre>`).join('');
    card.innerHTML = `
      <span class=close onclick=hideStats()>×</span>
      <h2>${id}</h2>
      <p><b>raw</b>: ${fmtBytes(d.raw_bytes)} · mtime ${d.raw_mtime || '?'}</p>
      <p><b>tokens</b>: ${d.total_tokens} · <b>vocab</b>: ${d.vocab_size}
         · TTR ${(d.type_token_ratio || 0).toFixed(4)}</p>
      ${headerWarn}${footerWarn}
      <p><b>top words</b>: <span style="color:#888">${top}</span></p>
      <p><b>sample</b>:</p>
      ${samples || '<p style="color:#666">(no samples)</p>'}
    `;
  }
  document.getElementById('stats-overlay').classList.add('show');
}
function hideStats() {
  document.getElementById('stats-overlay').classList.remove('show');
}
async function reprocess(id) {
  if (!confirm(`Reprocess ${id}? Strip headers + re-tokenize.`)) return;
  const r = await fetch(`/book/${id}/reprocess`, {method: 'POST'});
  const d = await r.json();
  alert(`${id}: ${JSON.stringify(d, null, 2)}`);
  loadLibrary();
}
async function deleteBook(id) {
  if (!confirm(`Delete ${id}? Removes raw/tokens/counts/metadata. Chroma index will reclaim on next reindex.`)) return;
  const r = await fetch(`/book/${id}/delete`, {method: 'POST'});
  const d = await r.json();
  alert(`${id}: ${JSON.stringify(d, null, 2)}`);
  loadLibrary();
}
async function triggerReindex() {
  if (!confirm('Запустить полную переиндексацию ChromaDB? Может занять много минут.')) return;
  const el = document.getElementById('reindex-status');
  el.textContent = 'запускаем…';
  try {
    const r = await fetch('/reindex', {method: 'POST'});
    const d = await r.json();
    if (d.started) {
      el.textContent = 'запущено в фоне (см. admin_reindex.log)';
      el.style.color = '#7ed321';
    } else {
      el.textContent = d.reason || 'не запущено';
      el.style.color = '#f5a623';
    }
  } catch (e) {
    el.textContent = 'error: ' + e;
    el.style.color = '#f08080';
  }
}
loadLibrary();
</script>
</body>
</html>
"""


def _render_failed_page() -> str:
    return _FAILED_PAGE


# Sprint 22+ — flagged-bad-answers admin page. Mirrors the /failed
# page pattern: JSON endpoint at /api/feedback, HTML at /feedback.
# Records loaded asynchronously from JSONL so the page stays responsive
# even with thousands of entries.
_FEEDBACK_PAGE = r"""<!doctype html>
<html lang=ru>
<head>
<meta charset=utf-8>
<title>wordcracker · flagged bad answers</title>
<style>
  body { font-family: ui-sans-serif, system-ui, sans-serif; background:#1c1f24; color:#eaeaea;
         max-width:1280px; margin:32px auto; padding:0 20px; }
  h1 { margin:0 0 6px 0; }
  .meta { color:#888; font-size:13px; margin-bottom:24px; }
  .nav { margin-bottom:16px; }
  .nav a { color:#7ed321; margin-right:14px; }
  .filter { background:#262a31; padding:10px 14px; border-radius:6px; margin-bottom:16px;
            display:flex; gap:14px; align-items:center; }
  .filter label { color:#aaa; font-size:13px; }
  .filter input, .filter select { background:#1a1d22; color:#eaeaea; border:1px solid #2f343c;
                                    padding:4px 8px; border-radius:4px; }
  table { border-collapse:collapse; width:100%; background:#262a31;
          border-radius:6px; overflow:hidden; }
  thead { background:#1a1d22; }
  th { text-align:left; padding:10px 14px; font-size:12px; color:#888;
       text-transform:uppercase; letter-spacing:0.5px; font-weight:normal;
       border-bottom:1px solid #2f343c; }
  td { padding:10px 14px; border-bottom:1px solid #1f2227; vertical-align:top;
       font-size:13px; }
  td.ts { color:#666; font-family:ui-monospace,monospace; font-size:11px;
          white-space:nowrap; }
  td.q { color:#a0c4ff; max-width:280px; }
  td.intent { color:#888; font-family:ui-monospace,monospace; font-size:12px;
              white-space:nowrap; }
  td.ans { color:#bbb; max-width:380px; font-size:12px; }
  td.note { color:#e08080; max-width:240px; font-size:12px; font-style:italic; }
  td.risk { white-space:nowrap; }
  td.risk .high   { background:#3a2020; color:#e08080; padding:2px 7px;
                    border-radius:9px; font-size:11px; }
  td.risk .medium { background:#3a3320; color:#e0a04e; padding:2px 7px;
                    border-radius:9px; font-size:11px; }
  td.risk .low    { background:#1e3a2e; color:#7ed321; padding:2px 7px;
                    border-radius:9px; font-size:11px; }
  td.expand { color:#666; font-family:ui-monospace,monospace; font-size:11px;
              cursor:pointer; }
  td.expand:hover { color:#7ed321; }
  .empty { color:#666; text-align:center; padding:40px 0; }
  details summary { cursor:pointer; color:#666; font-size:12px; }
  details summary:hover { color:#7ed321; }
  details pre { background:#0f1115; padding:8px; border-radius:4px;
                font-size:11px; max-height:300px; overflow:auto;
                white-space:pre-wrap; word-break:break-word; }
  .id-col { color:#555; font-family:ui-monospace,monospace; font-size:10px; }
  .total-card { background:#262a31; border-radius:6px; padding:12px 18px;
                margin-bottom:16px; display:flex; gap:24px; }
  .total-card .stat { display:flex; flex-direction:column; }
  .total-card .stat .v { color:#7ed321; font-size:22px; font-weight:bold;
                          font-variant-numeric:tabular-nums; }
  .total-card .stat .k { color:#888; font-size:11px; text-transform:uppercase; }
</style>
</head>
<body>
<h1>wordcracker · flagged bad answers</h1>
<div class=meta>Ответы, на которые пользователь нажал
🚩 «неправильный». Append-only JSONL в
<code>/workspace/spgc/derived/v2_feedback/bad-YYYY-MM-DD.jsonl</code>.
Новее — выше.</div>

<div class=nav>
  <a href="/">← upload</a>
  <a href="/failed">→ failed queries</a>
  <a href="/library/page">→ library</a>
  <a href="/api/feedback">JSON</a>
</div>

<div class=total-card>
  <div class=stat><span class=v id=stat-total>—</span><span class=k>flagged</span></div>
  <div class=stat><span class=v id=stat-risk-high>—</span><span class=k>high risk</span></div>
  <div class=stat><span class=v id=stat-risk-medium>—</span><span class=k>medium risk</span></div>
  <div class=stat><span class=v id=stat-with-note>—</span><span class=k>with user note</span></div>
</div>

<div class=filter>
  <label>intent:</label>
  <select id=intent-filter>
    <option value="">all</option>
  </select>
  <label>risk:</label>
  <select id=risk-filter>
    <option value="">all</option>
    <option value=high>high</option>
    <option value=medium>medium</option>
    <option value=low>low</option>
  </select>
  <label>has note:</label>
  <select id=note-filter>
    <option value="">all</option>
    <option value=yes>yes</option>
    <option value=no>no</option>
  </select>
  <span style="flex:1"></span>
  <span class=meta id=count>загрузка…</span>
</div>

<table id=tbl>
  <thead>
    <tr>
      <th>time</th>
      <th>intent</th>
      <th>query</th>
      <th>answer (truncated)</th>
      <th>user note</th>
      <th>risk</th>
      <th>details</th>
    </tr>
  </thead>
  <tbody id=body><tr><td colspan=7 class=empty>загрузка…</td></tr></tbody>
</table>

<script>
let RAW = [];

function fmtTs(iso) {
  if (!iso) return '';
  // Display as YYYY-MM-DD HH:MM:SS (UTC, but trim TZ for compactness)
  return iso.replace('T', ' ').split('.')[0];
}

function escapeHTML(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderRow(r) {
  const risk = (r.render_meta && r.render_meta.confabulation_risk) || '—';
  const riskCell = risk === '—'
    ? '<span style="color:#555">—</span>'
    : `<span class="${risk}">${risk}</span>`;
  const intent = r.intent || '—';
  const note = r.user_note || '';
  const ans = (r.answer || '').slice(0, 200);
  const detailsJSON = JSON.stringify(r, null, 2);
  return `<tr>
    <td class=ts>${escapeHTML(fmtTs(r.ts))}<br><span class=id-col>${escapeHTML(r.id||'')}</span></td>
    <td class=intent>${escapeHTML(intent)}</td>
    <td class=q>${escapeHTML(r.question||'')}</td>
    <td class=ans>${escapeHTML(ans)}${(r.answer||'').length>200?'…':''}</td>
    <td class=note>${escapeHTML(note)}</td>
    <td class=risk>${riskCell}</td>
    <td><details><summary>raw</summary><pre>${escapeHTML(detailsJSON)}</pre></details></td>
  </tr>`;
}

function applyFilters() {
  const intentF = document.getElementById('intent-filter').value;
  const riskF   = document.getElementById('risk-filter').value;
  const noteF   = document.getElementById('note-filter').value;
  let filtered = RAW.filter(r => {
    if (intentF && r.intent !== intentF) return false;
    const risk = r.render_meta && r.render_meta.confabulation_risk;
    if (riskF && risk !== riskF) return false;
    if (noteF === 'yes' && !r.user_note) return false;
    if (noteF === 'no' && r.user_note) return false;
    return true;
  });
  document.getElementById('count').textContent =
    filtered.length + ' / ' + RAW.length + ' shown';
  const tbody = document.getElementById('body');
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan=7 class=empty>пока ничего</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(renderRow).join('');
}

function updateStats() {
  document.getElementById('stat-total').textContent = RAW.length;
  document.getElementById('stat-risk-high').textContent =
    RAW.filter(r => r.render_meta && r.render_meta.confabulation_risk === 'high').length;
  document.getElementById('stat-risk-medium').textContent =
    RAW.filter(r => r.render_meta && r.render_meta.confabulation_risk === 'medium').length;
  document.getElementById('stat-with-note').textContent =
    RAW.filter(r => r.user_note).length;
}

function populateIntentFilter() {
  const select = document.getElementById('intent-filter');
  const intents = new Set(RAW.map(r => r.intent).filter(Boolean));
  // Keep first 'all' option, clear rest
  while (select.options.length > 1) select.remove(1);
  Array.from(intents).sort().forEach(i => {
    const opt = document.createElement('option');
    opt.value = i; opt.textContent = i;
    select.appendChild(opt);
  });
}

async function load() {
  try {
    const r = await fetch('/api/feedback?limit=1000&days=30');
    const data = await r.json();
    RAW = data.records || [];
    updateStats();
    populateIntentFilter();
    applyFilters();
  } catch (e) {
    document.getElementById('body').innerHTML =
      `<tr><td colspan=7 class=empty>ошибка загрузки: ${escapeHTML(e.message)}</td></tr>`;
  }
}

document.getElementById('intent-filter').addEventListener('change', applyFilters);
document.getElementById('risk-filter').addEventListener('change', applyFilters);
document.getElementById('note-filter').addEventListener('change', applyFilters);
load();
// Refresh once a minute — admin keeps the tab open while triaging
setInterval(load, 60000);
</script>
</body>
</html>
"""


def _render_feedback_page() -> str:
    return _FEEDBACK_PAGE


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
            # ADR-B3 / D-SB3-2: /health is JSON carrying git_sha +
            # build_time + version. Same shape as chat_server's /health
            # (both serve under wordcracker-textlab:<sha>), so
            # verify_deployed_image.sh can probe both and assert the
            # process inside each container matches the expected SHA.
            from scripts.v2.__version__ import runtime_identity
            return self._json(200, runtime_identity())
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
        # v2.7: failed-query log. Admin can see «what users asked that
        # we couldn't answer» — clarify + out_of_scope rows from the
        # chat ring buffer, newest first. JSON for tooling, HTML for
        # eyeballs.
        if self.path in ("/api/failed", "/api/failed_queries"):
            try:
                # _combined variants read in-memory ring buffer + on-disk
                # JSONL. Admin runs as a separate `docker compose exec`
                # python process from chat_server, so it sees an empty
                # ring; on-disk reads pick up chat's logged failures.
                from scripts.v2.observability import (
                    recent_failures_combined, top_failed_phrases_combined,
                )
                return self._json(200, {
                    "failed": recent_failures_combined(limit=100),
                    "top_phrases": top_failed_phrases_combined(top_n=15),
                })
            except ImportError as e:
                return self._json(503, {
                    "error": "observability module not available",
                    "detail": str(e),
                    "hint": ("admin_server didn't add repo root to "
                             "sys.path — check imports"),
                })
            except Exception as e:
                import traceback
                return self._json(500, {
                    "error": "failed-query log unavailable",
                    "type": type(e).__name__,
                    "detail": str(e),
                    "trace": traceback.format_exc().splitlines()[-5:],
                })
        if self.path == "/failed":
            return self._html(_render_failed_page())
        # Sprint 22+ — user-flagged bad answers («🚩 неправильный»
        # button). Same shape as /failed: JSON for tooling, HTML page
        # for eyeballs. Records live in append-only JSONL at
        # /workspace/spgc/derived/v2_feedback/bad-YYYY-MM-DD.jsonl.
        if (self.path == "/api/feedback"
                or self.path.startswith("/api/feedback?")):
            try:
                from scripts.v2.feedback import list_recent
            except ImportError as e:
                return self._json(503, {
                    "error": "feedback module unavailable",
                    "detail": str(e),
                })
            limit, days = 200, 14
            if "?" in self.path:
                from urllib.parse import parse_qs
                params = parse_qs(self.path.split("?", 1)[1])
                try:
                    limit = max(1, min(int(params.get("limit", ["200"])[0]),
                                         1000))
                except (ValueError, IndexError):
                    pass
                try:
                    days = max(1, min(int(params.get("days", ["14"])[0]), 90))
                except (ValueError, IndexError):
                    pass
            try:
                records = list_recent(days_back=days, limit=limit)
                return self._json(200, {"count": len(records),
                                          "records": records})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if self.path == "/feedback":
            return self._html(_render_feedback_page())
        # Sprint 19 — admin library endpoints (list / inspect / download)
        if self.path in ("/library", "/api/library"):
            try:
                from admin_library import list_user_books
                return self._json(200, {"books": list_user_books()})
            except Exception as e:
                return self._json(500, {"error": str(e)})
        if self.path == "/library/page":
            return self._html(_render_library_page())
        if self.path in ("/audit", "/api/audit"):
            try:
                from admin_library import audit_library
                return self._json(200, audit_library())
            except Exception as e:
                return self._json(500, {"error": str(e)})
        # /book/<u_id>/stats — JSON stats
        m = re.match(r"^/book/([Uu]\d+)/stats$", self.path)
        if m:
            try:
                from admin_library import book_stats
                return self._json(200, book_stats(m.group(1)))
            except Exception as e:
                return self._json(500, {"error": str(e)})
        # /book/<u_id>/raw — text download
        m = re.match(r"^/book/([Uu]\d+)/raw$", self.path)
        if m:
            try:
                from admin_library import book_raw_path
                p = book_raw_path(m.group(1))
                if p is None:
                    return self._json(404, {"error": "book not found"})
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{m.group(1).upper()}.txt"',
                )
                self.end_headers()
                self.wfile.write(body)
                return
            except Exception as e:
                return self._json(500, {"error": str(e)})
        return self._html(PAGE)

    def do_POST(self):
        # Sprint 19 — admin library mutations (delete / reprocess)
        m = re.match(r"^/book/([Uu]\d+)/delete$", self.path)
        if m:
            try:
                from admin_library import delete_book
                return self._json(200, delete_book(m.group(1)))
            except Exception as e:
                return self._json(500, {"error": str(e)})
        m = re.match(r"^/book/([Uu]\d+)/reprocess$", self.path)
        if m:
            try:
                from admin_library import reprocess_book
                return self._json(200, reprocess_book(m.group(1)))
            except Exception as e:
                return self._json(500, {"error": str(e)})
        # Sprint 19 — reindex trigger from library page (after delete /
        # reprocess the admin can refresh Chroma without going through
        # the upload tab).
        if self.path == "/reindex":
            try:
                return self._json(200, _trigger_reindex_async())
            except Exception as e:
                return self._json(500, {"error": str(e)})

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
