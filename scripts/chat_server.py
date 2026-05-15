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
from rag_query import ask, ask_stream, SYSTEM_PROMPT, ASSISTANT_NAME
from rag_tools import TOOLS_SPEC

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
  <span class=meta>qwen3:14b · 11 tools · agentic loop · multi-turn</span>
  <span style="flex:1"></span>
  <button class=secondary type=button onclick="clearHistory()">clear</button>
</header>
<div id=log></div>
<div class=hint>Примеры: «дай статистику по Wodehouse», «топ-15 биграмм Достоевского», «фирменные слова Doyle», «найди упоминания битой посуды», «сравни Wodehouse и Twain»</div>
<form id=f>
  <textarea id=q placeholder="Ctrl+Enter — отправить" autofocus></textarea>
  <button id=send>send</button>
</form>

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
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body_bytes)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if self.path == "/api/tools":
            tools = [{"name": t["function"]["name"],
                      "description": t["function"]["description"]} for t in TOOLS_SPEC]
            return self._send(200, json.dumps(tools, ensure_ascii=False, indent=2),
                              "application/json; charset=utf-8")
        page = PAGE.replace("__ASSISTANT_NAME__", ASSISTANT_NAME)
        return self._send(200, page, "text/html; charset=utf-8")

    def _stream_chat(self, payload):
        """SSE endpoint — pump ask_stream events to the client."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
        self.end_headers()

        question = (payload.get("question") or "").strip()
        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        if not question:
            self.wfile.write(b'data: {"event":"error","message":"empty question"}\n\n')
            return
        try:
            for ev in ask_stream(question, history=history):
                line = "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                try:
                    self.wfile.flush()
                except Exception:
                    return  # client gone
        except Exception as e:
            try:
                self.wfile.write(
                    f'data: {{"event":"error","message":"server: {e}"}}\n\n'.encode("utf-8"))
            except Exception:
                pass

    def do_POST(self):
        if self.path == "/api/chat/stream":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length))
            except Exception:
                return self._send(400, b'{"error":"bad json"}', "application/json")
            return self._stream_chat(payload)
        if self.path != "/api/chat":
            return self._send(404, b"not found", "text/plain")
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            return self._send(400, json.dumps({"error": "bad json"}), "application/json")
        question = (payload.get("question") or "").strip()
        if not question:
            return self._send(400, json.dumps({"error": "empty question"}), "application/json")

        history = payload.get("history") or []
        if not isinstance(history, list):
            history = []
        t0 = time.time()
        try:
            res = ask(question, history=history)
        except Exception as e:
            return self._send(500, json.dumps({"error": f"ask() raised: {e}"}),
                              "application/json")
        print(f"[chat] {question[:60]!r} → {res['iterations']} iter, "
              f"{len(res['tool_calls'])} call(s), {res['elapsed_sec']}s "
              f"(wall {time.time()-t0:.1f}s)", file=sys.stderr)
        return self._send(200, json.dumps(res, ensure_ascii=False, default=str),
                          "application/json; charset=utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8890)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"wordcracker chat on http://{args.host}:{args.port}/")
    srv.serve_forever()


if __name__ == "__main__":
    main()
