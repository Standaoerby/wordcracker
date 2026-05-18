#!/usr/bin/env python3
"""
chat_server.py — minimal web chat for the wordcracker LLM agent.

Lives inside the gutenberg-lab container so it has direct access to
rag_query.ask, ChromaDB and the SPGC dump. The HTML page keeps history
in localStorage on the client side; the server is stateless.

Routes:
  GET  /                — chat HTML
  POST /api/chat        — body: {"question": str, "history": [...]} → {"answer", "tool_calls", ...}
  GET  /api/tools       — list of available tools (for the UI)
  GET  /health          — "ok"

Run inside the container (via docker compose):
  python /workspace/scripts/chat_server.py --port 8890
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root for scripts.v2.*
from rag_query import ask as ask_v1, ask_stream as ask_stream_v1, SYSTEM_PROMPT, ASSISTANT_NAME
from rag_query import TOOLS_SPEC  # combined rag_tools + learning_tools

# v2 engine — lazy-imported on first request that asks for it. Avoids slow
# import cost for users on the v1 path and keeps v1 deployable even if v2 has
# a bug.
_V2_ENGINE = {"ask": None, "ask_stream": None, "loaded": False}


def _v2_engine():
    if _V2_ENGINE["loaded"]:
        return _V2_ENGINE
    try:
        from scripts.v2.rag_v2 import ask as v2_ask, ask_stream as v2_ask_stream
        _V2_ENGINE["ask"] = v2_ask
        _V2_ENGINE["ask_stream"] = v2_ask_stream
    except Exception as e:
        print(f"[chat] v2 engine unavailable: {e}", file=sys.stderr, flush=True)
        _V2_ENGINE["ask"] = None
        _V2_ENGINE["ask_stream"] = None
    _V2_ENGINE["loaded"] = True
    return _V2_ENGINE


def _pick_engine(path: str, headers, payload: dict) -> str:
    """Return 'v1' or 'v2' based on (in order): query string ?engine=, header
    X-WC-Engine:, payload['engine'], env WC_DEFAULT_ENGINE, fallback 'v1'."""
    import urllib.parse as up
    qs = up.urlparse(path).query
    q = up.parse_qs(qs)
    eng = (q.get("engine", [None])[0]
           or headers.get("X-WC-Engine")
           or payload.get("engine")
           or os.environ.get("WC_DEFAULT_ENGINE", "v1")).lower()
    return "v2" if eng == "v2" else "v1"


def ask(question, history=None, *, engine="v1", **kw):
    if engine == "v2":
        v2 = _v2_engine()
        if v2["ask"] is not None:
            return v2["ask"](question, history=history, **kw)
    return ask_v1(question, history=history, **kw)


def ask_stream(question, history=None, *, engine="v1", **kw):
    if engine == "v2":
        v2 = _v2_engine()
        if v2["ask_stream"] is not None:
            yield from v2["ask_stream"](question, history=history, **kw)
            return
    yield from ask_stream_v1(question, history=history, **kw)


import os  # noqa: E402 — must follow the import block above

# Adversarial-input caps. Normal chat traffic doesn't come anywhere near
# these — they exist to bound damage from malicious / mistaken clients
# (multi-MB payloads, 1000-message histories, recursive JSON, prompt
# injection via giant context bombs).
MAX_PAYLOAD_BYTES   = 64 * 1024     # ~64 KB request body
MAX_QUESTION_CHARS  = 4_000         # ~5× the longest Q40 from the vault
MAX_HISTORY_TURNS   = 50            # past 50 turns = ~1 hour conversation
MAX_HISTORY_BYTES   = 64 * 1024     # 64 KB clipped from tail

PAGE = r"""<!doctype html>
<html lang=ru>
<head>
<meta charset=utf-8>
<title>__ASSISTANT_NAME__ · wordcracker</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
         background:#1c1f24; color:#eaeaea; margin:0;
         display:flex; flex-direction:column; height:100vh; }
  header { padding:12px 18px; border-bottom:1px solid #2f343c; display:flex;
           align-items:center; gap:12px; }
  header h1 { margin:0; font-size:16px; }
  header .meta { color:#888; font-size:12px; }
  .pill { background:#262a31; padding:2px 8px; border-radius:10px; font-size:11px; color:#7ed321; }
  #log { flex:1; overflow-y:auto; padding:16px 18px; }
  .msg { margin-bottom:14px; }
  .msg.user { color:#a0c4ff; }
  .msg.user::before { content:"› "; color:#557; }
  .msg.assistant { background:#262a31; border-left:3px solid #50e3c2;
                   padding:10px 14px; border-radius:6px; }
  .msg.error { background:#3b1f22; border-left:3px solid #e05a5a; padding:10px 14px; }
  .meta-row { color:#888; font-size:11px; margin-top:6px; font-variant-numeric:tabular-nums; }
  details.trace { margin-top:6px; color:#888; font-size:12px; }
  details.trace summary { cursor:pointer; user-select:none; }
  details.trace pre { background:#1a1d22; padding:8px; border-radius:4px;
                      overflow-x:auto; font-size:11px; max-height:200px; }
  .live-status { color:#888; font-size:13px; padding:6px 0; }
  .live-status .row { display:flex; gap:8px; align-items:baseline; margin-bottom:4px;
                      font-family:ui-monospace,monospace; font-size:12px; }
  .live-status .row .ico { width:18px; }
  .live-status .row.run    .ico::after { content:"⏳"; animation:spin 1.5s linear infinite; display:inline-block; }
  .live-status .row.ok     .ico::after { content:"✅"; }
  .live-status .row.fail   .ico::after { content:"❌"; }
  .live-status .row .args  { color:#666; }
  .live-status .row .ms    { margin-left:auto; color:#7ed321; font-variant-numeric:tabular-nums; }
  .live-status .iter       { color:#50e3c2; font-size:11px; margin-top:6px; }
  .live-status .think      { color:#50e3c2; }
  .live-status .think::after { content:"…"; animation:dots 1.2s steps(4) infinite; }
  /* Sprint 10: badges shown next to the assistant bubble */
  .badges { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; font-size:11px; }
  .badge { padding:2px 7px; border-radius:9px; font-variant-numeric:tabular-nums; }
  .badge.intent  { background:#2d3a4d; color:#a0c4ff; }
  .badge.critic.ok    { background:#1e3a2e; color:#7ed321; }
  .badge.critic.warn  { background:#3a3320; color:#e0a04e; }
  .badge.engine  { background:#2a2630; color:#b599e5; }
  .copy-btn      { background:transparent; color:#888; border:1px solid #2f343c;
                   font-size:11px; padding:2px 8px; border-radius:4px; cursor:pointer; }
  .copy-btn:hover { color:#eaeaea; border-color:#50e3c2; }
  /* Sprint 11.5 retry-with-scope button — shown when planner asked clarify */
  .retry-scope   { background:transparent; color:#a0c4ff; border:1px solid #2d3a4d;
                   font-size:11px; padding:2px 8px; border-radius:4px; cursor:pointer;
                   margin-top:6px; }
  .retry-scope:hover { color:#eaeaea; border-color:#a0c4ff; }
  /* Sprint 11.5 sticky stats footer */
  #stats-footer  { padding:4px 18px; border-top:1px solid #2f343c; background:#161a1f;
                   color:#666; font-size:11px; font-variant-numeric:tabular-nums;
                   display:flex; gap:14px; align-items:center; }
  #stats-footer .stat-key { color:#555; }
  #stats-footer .stat-val { color:#a0c4ff; }
  #stats-footer .stat-warn { color:#e0a04e; }
  @keyframes dots { 0% { content:""; } 25% { content:"."; } 50% { content:".."; } 75% { content:"..."; } }
  @keyframes spin { to { transform: rotate(360deg); } }
  table { border-collapse:collapse; margin:8px 0; }
  th, td { border:1px solid #3a3f48; padding:4px 10px; text-align:left; }
  th { background:#2f343c; }
  code { background:#1a1d22; padding:1px 4px; border-radius:3px; font-size:90%; }
  pre  { background:#1a1d22; padding:8px; border-radius:4px; overflow-x:auto; }
  form { display:flex; gap:8px; padding:12px 18px; border-top:1px solid #2f343c;
         background:#161a1f; }
  textarea { flex:1; background:#1a1d22; color:#eaeaea; border:1px solid #2f343c;
             border-radius:4px; padding:8px 10px; font-family:inherit; resize:none;
             min-height:42px; max-height:200px; }
  button { background:#50e3c2; border:0; color:#0a0c10; font-weight:600;
           padding:0 18px; border-radius:4px; cursor:pointer; }
  button:disabled { opacity:0.5; cursor:wait; }
  button.secondary { background:transparent; color:#888; border:1px solid #2f343c;
                     font-weight:normal; padding:0 12px; }
  a { color:#7ed321; }
  .hint { color:#666; font-size:12px; padding:0 18px 8px 18px; }
</style>
</head>
<body>
<header>
  <h1>__ASSISTANT_NAME__ · wordcracker</h1>
  <span class=meta>v2 engine · wordcracker:v2 · planner→router→renderer→critic</span>
  <span style="flex:1"></span>
  <button class=secondary type=button onclick="clearHistory()">clear</button>
</header>
<div id=log></div>
<div class=hint>Примеры: «дай статистику по Wodehouse», «топ-15 биграмм Достоевского», «фирменные слова Doyle», «найди упоминания битой посуды», «сравни Wodehouse и Twain»</div>
<form id=f>
  <textarea id=q placeholder="Ctrl+Enter — отправить" autofocus></textarea>
  <button id=send>send</button>
</form>
<div id=stats-footer>
  <span class=stat-key>queries:</span><span id=stat-total class=stat-val>—</span>
  <span class=stat-key>avg:</span><span id=stat-avg class=stat-val>—</span>
  <span class=stat-key>cache:</span><span id=stat-cache class=stat-val>—</span>
  <span class=stat-key>critic flags:</span><span id=stat-critic class=stat-val>—</span>
  <span style="flex:1"></span>
  <span class=stat-key id=stat-window>last 256 reqs</span>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script>
const log = document.getElementById('log');
const q   = document.getElementById('q');
const send = document.getElementById('send');
const HKEY = 'wordcracker_chat_history_v1';

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HKEY)) || []; } catch { return []; }
}
function saveHistory(h) { localStorage.setItem(HKEY, JSON.stringify(h)); }
function clearHistory() {
  if (confirm('Очистить историю?')) {
    localStorage.removeItem(HKEY);
    log.innerHTML = '';
  }
}

function render(role, text, extras) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (role === 'assistant') {
    div.innerHTML = marked.parse(text || '');
    if (extras) {
      const meta = document.createElement('div');
      meta.className = 'meta-row';
      meta.textContent = `${extras.iterations} iter · ${extras.elapsed_sec}s · ${extras.tool_calls.length} call(s)`;
      div.appendChild(meta);
      if (extras.tool_calls.length) {
        const det = document.createElement('details');
        det.className = 'trace';
        det.innerHTML = '<summary>tool trace</summary>';
        const pre = document.createElement('pre');
        pre.textContent = extras.tool_calls.map(tc =>
          `· ${tc.name}(${JSON.stringify(tc.args)})\n  → ${tc.result_summary}`
        ).join('\n\n');
        det.appendChild(pre);
        div.appendChild(det);
      }
    }
  } else {
    div.textContent = text;
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function renderError(msg) {
  const div = document.createElement('div');
  div.className = 'msg error';
  div.textContent = msg;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function submit(text) {
  const history = loadHistory();
  history.push({role: 'user', content: text});
  saveHistory(history);
  render('user', text);

  send.disabled = true;
  send.textContent = '…';

  // create a live-status block we update as SSE events arrive
  const live = document.createElement('div');
  live.className = 'msg assistant';
  const status = document.createElement('div');
  status.className = 'live-status';
  status.innerHTML = '<div class=think>думаю</div>';
  live.appendChild(status);
  log.appendChild(live);
  log.scrollTop = log.scrollHeight;

  const t0 = Date.now();
  const timer = setInterval(() => {
    const el = status.querySelector('.think');
    if (el) el.textContent = 'думаю (' + ((Date.now()-t0)/1000).toFixed(1) + 's)';
  }, 200);

  const toolRows = {};   // name → row element (last-pending)
  const tools = [];
  let intentLabel = null;   // captured from {event:'intent'} early in stream
  let criticInfo  = null;   // captured from {event:'critic'} late in stream

  try {
    const r = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: text, history: history.slice(0, -1)}),
    });
    if (!r.ok || !r.body) throw new Error('HTTP ' + r.status);
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let answerText = '';
    let final;

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream:true});
      // SSE messages: lines of "data: <json>\n\n"
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx); buffer = buffer.slice(idx+2);
        const line = raw.startsWith('data: ') ? raw.slice(6) : raw;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        switch (ev.event) {
          case 'start':
            status.querySelector('.think').textContent = 'думаю';
            break;
          case 'iter': {
            const row = document.createElement('div');
            row.className = 'iter';
            row.textContent = '— iteration ' + ev.n + ' —';
            status.appendChild(row);
            break;
          }
          case 'tool_call': {
            const row = document.createElement('div');
            row.className = 'row run';
            row.innerHTML = '<span class=ico></span><b></b><span class=args></span>';
            row.querySelector('b').textContent = ev.name;
            row.querySelector('.args').textContent =
              ' ' + JSON.stringify(ev.args).slice(0, 80);
            status.appendChild(row);
            toolRows[ev.name] = row;
            tools.push({name: ev.name, args: ev.args});
            break;
          }
          case 'tool_result': {
            const row = toolRows[ev.name];
            if (row) {
              row.classList.remove('run');
              row.classList.add('ok');
              const ms = document.createElement('span'); ms.className = 'ms';
              ms.textContent = ev.ms + ' ms';
              row.appendChild(ms);
            }
            tools[tools.length-1].result_summary = ev.summary;
            break;
          }
          case 'intent':
            intentLabel = ev.label;
            // Update the live-status header so the user sees the planner
            // verdict immediately.
            const ihint = status.querySelector('.think');
            if (ihint) ihint.textContent = 'planner: ' + ev.label;
            break;
          case 'critic':
            criticInfo = ev;
            break;
          case 'answer':
            answerText = ev.text;
            break;
          case 'done':
            final = ev;
            break;
          case 'error':
            renderError(ev.message);
            return;
        }
      }
    }

    // upgrade the live block to the proper assistant bubble
    clearInterval(timer);
    live.innerHTML = marked.parse(answerText || '');

    // Sprint 10 badges row: engine / intent / critic / copy button.
    const badges = document.createElement('div');
    badges.className = 'badges';
    const eng = document.createElement('span');
    eng.className = 'badge engine'; eng.textContent = 'v2';
    badges.appendChild(eng);
    if (intentLabel) {
      const b = document.createElement('span');
      b.className = 'badge intent'; b.textContent = 'intent: ' + intentLabel;
      badges.appendChild(b);
    }
    if (criticInfo) {
      const b = document.createElement('span');
      b.className = 'badge critic ' + (criticInfo.issues_flagged ? 'warn' : 'ok');
      b.title = criticInfo.summary || '';
      b.textContent = criticInfo.issues_flagged
        ? `⚠ critic: ${criticInfo.unsupported_claims_n} flag(s)`
        : '✓ critic clean';
      badges.appendChild(b);
    }
    if (answerText) {
      const copyBtn = document.createElement('button');
      copyBtn.className = 'copy-btn'; copyBtn.type = 'button';
      copyBtn.textContent = 'copy';
      copyBtn.onclick = () => {
        navigator.clipboard.writeText(answerText);
        copyBtn.textContent = 'copied ✓';
        setTimeout(() => copyBtn.textContent = 'copy', 1200);
      };
      badges.appendChild(copyBtn);
    }
    live.appendChild(badges);

    // Sprint 11.5: retry-with-scope on clarify. Pre-fill the textarea with
    // the last user question so Stan can append the missing scope (author,
    // book, period) and re-send without retyping. Cheap UX win — clarify
    // answers happen ~3-4/40 in the bench, and retyping the long Q40
    // pasted from the vault is annoying.
    if (intentLabel === 'clarify') {
      const retryBtn = document.createElement('button');
      retryBtn.className = 'retry-scope'; retryBtn.type = 'button';
      retryBtn.textContent = '↻ уточнить и переспросить';
      retryBtn.onclick = () => {
        q.value = text;
        q.focus();
        q.setSelectionRange(text.length, text.length);
      };
      live.appendChild(retryBtn);
    }

    if (final) {
      const meta = document.createElement('div');
      meta.className = 'meta-row';
      meta.textContent = `${final.iterations} iter · ${final.elapsed_sec}s · ${final.tool_calls.length} call(s)`;
      live.appendChild(meta);
      if (final.tool_calls.length) {
        const det = document.createElement('details');
        det.className = 'trace';
        det.innerHTML = '<summary>tool trace</summary>';
        const pre = document.createElement('pre');
        pre.textContent = final.tool_calls.map(tc =>
          `· ${tc.name}(${JSON.stringify(tc.args)})\n  → ${tc.result_summary}`
        ).join('\n\n');
        det.appendChild(pre);
        live.appendChild(det);
      }
    }
    history.push({role: 'assistant', content: answerText});
    saveHistory(history);
    log.scrollTop = log.scrollHeight;
  } catch (e) {
    clearInterval(timer);
    live.remove();
    renderError('Stream error: ' + e.message);
  } finally {
    send.disabled = false;
    send.textContent = 'send';
    q.focus();
  }
}

// Sprint 11.5: poll /api/stats every 30s so the sticky footer shows live
// counters from the in-process ring buffer (queries today, avg latency,
// cache hit rate, critic flags). Failures are silent — footer just keeps
// the last successful values.
async function refreshStats() {
  try {
    const r = await fetch('/api/stats');
    if (!r.ok) return;
    const s = await r.json();
    const total = s.total || 0;
    document.getElementById('stat-total').textContent = total;
    if (total > 0) {
      const avgMs = s.avg_elapsed_ms || 0;
      document.getElementById('stat-avg').textContent = (avgMs / 1000).toFixed(1) + 's';
      const cacheRate = s.cache_hit_rate || 0;
      document.getElementById('stat-cache').textContent =
        (cacheRate * 100).toFixed(0) + '% (' + (s.cache_hits || 0) + '/' + (s.cache_calls || 0) + ')';
      const cflag = s.critic_flagged || 0;
      const cel = document.getElementById('stat-critic');
      cel.textContent = cflag + '/' + total;
      cel.className = cflag > total * 0.3 ? 'stat-warn' : 'stat-val';
    } else {
      document.getElementById('stat-avg').textContent = '—';
      document.getElementById('stat-cache').textContent = '—';
      document.getElementById('stat-critic').textContent = '—';
    }
  } catch (e) { /* silent */ }
}
refreshStats();
setInterval(refreshStats, 30000);

// restore history
for (const m of loadHistory()) {
  render(m.role, m.content);
}

document.getElementById('f').addEventListener('submit', e => {
  e.preventDefault();
  const text = q.value.trim();
  if (!text) return;
  q.value = '';
  submit(text);
});
q.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    document.getElementById('f').requestSubmit();
  }
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        return

    def _send(self, code, body, ctype):
        body_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_bytes)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the connection mid-response

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if self.path == "/api/tools":
            tools = [{"name": t["function"]["name"],
                      "description": t["function"]["description"]} for t in TOOLS_SPEC]
            return self._send(200, json.dumps(tools, ensure_ascii=False, indent=2),
                              "application/json; charset=utf-8")
        if self.path == "/api/stats":
            # Sprint 11.5: stats footer polls this every 30s. Aggregates the
            # in-process ring buffer of the last 256 v2 requests. Empty when
            # the engine is v1 or the ring buffer is fresh after restart.
            try:
                from scripts.v2.observability import aggregate_recent
                payload = aggregate_recent()
            except ImportError:
                payload = {"total": 0, "engine": "v1"}
            return self._send(200, json.dumps(payload, ensure_ascii=False, default=str),
                              "application/json; charset=utf-8")
        page = PAGE.replace("__ASSISTANT_NAME__", ASSISTANT_NAME)
        return self._send(200, page, "text/html; charset=utf-8")

    def _stream_chat(self, payload):
        """SSE endpoint — pump ask_stream events to the client."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return  # client already gone before we wrote a byte

        question, history, err = self._sanitize_chat_payload(payload)
        if err:
            try:
                msg = json.dumps({"event": "error", "message": err},
                                 ensure_ascii=False)
                self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        engine = _pick_engine(self.path, self.headers, payload)
        try:
            for ev in ask_stream(question, history=history, engine=engine):
                line = "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
                try:
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return  # client gone — stop pumping events
                except Exception:
                    return  # client gone (TLS error, etc.)
        except Exception as e:
            try:
                self.wfile.write(
                    f'data: {{"event":"error","message":"server: {e}"}}\n\n'.encode("utf-8"))
            except Exception:
                pass

    def _read_payload_capped(self) -> tuple[dict, str | None]:
        """Read POST body with a hard cap and return parsed JSON or an error.

        Caps:
          * Content-Length ≤ MAX_PAYLOAD_BYTES (64 KB) — normal chat payloads
            are < 1 KB; 64 KB tolerates a hand-pasted Q40 with a 100-item
            history. Anything larger is almost certainly an attempt to DoS
            the LLM or the JSON parser with a giant blob.
          * Body is read in one shot from rfile but capped, so we never burn
            unbounded memory on a malicious Content-Length: 1000000000 header.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return {}, "bad content-length"
        if length > MAX_PAYLOAD_BYTES:
            return {}, (f"payload too large ({length} > {MAX_PAYLOAD_BYTES} bytes); "
                        f"trim history or split into multiple queries")
        if length < 0:
            return {}, "negative content-length"
        raw = self.rfile.read(min(length, MAX_PAYLOAD_BYTES))
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}, "bad json"
        if not isinstance(payload, dict):
            return {}, "payload must be a JSON object"
        return payload, None

    def _sanitize_chat_payload(self, payload: dict) -> tuple[str, list, str | None]:
        """Validate + clip question/history. Returns (question, history, err)."""
        question = (payload.get("question") or "")
        if not isinstance(question, str):
            return "", [], "question must be a string"
        question = question.strip()
        if not question:
            return "", [], "empty question"
        if len(question) > MAX_QUESTION_CHARS:
            return "", [], (f"question too long ({len(question)} > "
                            f"{MAX_QUESTION_CHARS} chars); split into smaller asks")
        # Drop control chars except newline / tab — they have no business
        # in a chat prompt and screw up downstream regex matching.
        question = "".join(ch for ch in question
                           if ch in "\n\t" or ord(ch) >= 32)

        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        # Cap depth (50 turns is way past any reasonable convo) and total
        # size (64 KB stops attacker from threading a 10 MB context bomb).
        if len(history) > MAX_HISTORY_TURNS:
            history = history[-MAX_HISTORY_TURNS:]
        total = 0
        clipped: list = []
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            size = len(json.dumps(item, ensure_ascii=False, default=str))
            if total + size > MAX_HISTORY_BYTES:
                break
            clipped.append(item)
            total += size
        history = list(reversed(clipped))
        return question, history, None

    def do_POST(self):
        if self.path == "/api/chat/stream":
            payload, err = self._read_payload_capped()
            if err:
                return self._send(400, json.dumps({"error": err}).encode("utf-8"),
                                  "application/json")
            return self._stream_chat(payload)
        if not (self.path == "/api/chat" or self.path.startswith("/api/chat?")):
            return self._send(404, b"not found", "text/plain")
        payload, err = self._read_payload_capped()
        if err:
            return self._send(400, json.dumps({"error": err}).encode("utf-8"),
                              "application/json")
        question, history, err = self._sanitize_chat_payload(payload)
        if err:
            return self._send(400, json.dumps({"error": err}).encode("utf-8"),
                              "application/json")
        engine = _pick_engine(self.path, self.headers, payload)
        t0 = time.time()
        try:
            res = ask(question, history=history, engine=engine)
        except Exception as e:
            return self._send(500, json.dumps({"error": f"ask() raised: {e}"}),
                              "application/json")
        print(f"[chat:{engine}] {question[:60]!r} → {res['iterations']} iter, "
              f"{len(res['tool_calls'])} call(s), {res['elapsed_sec']}s "
              f"(wall {time.time()-t0:.1f}s)", file=sys.stderr)
        return self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                          "application/json; charset=utf-8")


def _warmup():
    """Pre-load ChromaDB + SentenceTransformer on cuda so the first user
    query that hits semantic_search or word_contexts_global doesn't pay the
    30+ second cold-load cost. Logs timing but never crashes the server —
    if warmup fails (e.g. chromadb file is being rebuilt), the tools will
    just take their first call slow path as they did before."""
    import sys as _sys, time as _time
    try:
        from rag_tools import _get_chroma_collection_with_embedder
        t = _time.perf_counter()
        col = _get_chroma_collection_with_embedder()
        # Touch the index with a one-result query so HNSW segments load.
        try:
            col.query(query_texts=["warmup"], n_results=1)
        except Exception:
            pass
        print(f"[chat] chromadb+embedder warmed in {_time.perf_counter()-t:.1f}s",
              file=_sys.stderr, flush=True)
    except Exception as e:
        print(f"[chat] warmup failed (non-fatal): {type(e).__name__}: {e}",
              file=_sys.stderr, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip ChromaDB/SentenceTransformer warmup at startup")
    args = ap.parse_args()
    if not args.no_warmup:
        _warmup()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"wordcracker chat on http://{args.host}:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
