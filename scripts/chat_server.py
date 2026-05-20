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
    """Return 'v1' or 'v2'.

    By default the engine is locked to `WC_DEFAULT_ENGINE` (= v2 in prod).
    Client-supplied hints (`?engine=v1`, `X-WC-Engine` header,
    `payload['engine']`) are IGNORED unless `WC_ALLOW_ENGINE_OVERRIDE=1`
    is set — they used to be honored, which gave anyone with the chat URL
    a free path around the v2 planner's input caps + prompt-injection
    guards by appending `?engine=v1`. Strict locking is the safer default.

    Set `WC_ALLOW_ENGINE_OVERRIDE=1` to bring back the legacy behavior
    (useful for A/B testing or for the v1 fallback flag the runner uses).
    """
    default = (os.environ.get("WC_DEFAULT_ENGINE", "v1")).lower()
    if os.environ.get("WC_ALLOW_ENGINE_OVERRIDE", "0") != "1":
        return "v2" if default == "v2" else "v1"
    import urllib.parse as up
    qs = up.urlparse(path).query
    q = up.parse_qs(qs)
    eng = (q.get("engine", [None])[0]
           or headers.get("X-WC-Engine")
           or payload.get("engine")
           or default).lower()
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
  /* Sprint 22+ — feedback button «🚩 неправильный». Discoverable but
     not loud — soft red so users notice it's available without it
     screaming at them on every correct answer. */
  .flag-btn      { background:transparent; color:#888; border:1px solid #2f343c;
                   font-size:11px; padding:2px 8px; border-radius:4px; cursor:pointer; }
  .flag-btn:hover { color:#e08080; border-color:#7a3a3a; }
  .flag-btn:disabled { opacity:0.6; cursor:wait; }
  .flag-btn-sent { color:#7ed321 !important; border-color:#1e3a2e !important; }
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
  /* v2.5 demo polish: clickable suggestion chips on empty chat */
  /* v2.8 — chips stay visible после первого submit (collapsed by default) */
  #suggestions { padding:14px 18px 10px 18px; display:flex; flex-wrap:wrap;
                 gap:8px; }
  #suggestions .chip { background:#262a31; color:#a0c4ff;
                       border:1px solid #2f343c; padding:6px 12px;
                       border-radius:14px; font-size:13px;
                       cursor:pointer; transition:all 0.15s; }
  #suggestions .chip:hover { background:#2d3a4d; border-color:#a0c4ff;
                              color:#eaeaea; }
  #suggestions h3 { margin:0 0 8px 0; width:100%; font-size:12px;
                    color:#888; font-weight:normal; text-transform:uppercase;
                    letter-spacing:0.5px; }
  #suggestions.collapsed { padding:6px 18px 0 18px; }
  #suggestions.collapsed > div { display:none; }
  #suggestions.collapsed::before { content:"💡 примеры запросов"; color:#666;
                                    font-size:12px; cursor:pointer; }
  #suggestions.collapsed:hover::before { color:#a0c4ff; }
  /* Sprint 18 — pure-CSS auto-hide when any message is in the log.
     Bullets through history-state edge cases (page load before localStorage
     hydration, manual clear+immediate-submit, browser cache of old JS).
     Sibling selector — relies on #log and #suggestions being adjacent.
     `force-show` class overrides for the user's manual expand. */
  #log:not(:empty) ~ #suggestions:not(.force-show) { display:none; }
  #suggestions:empty { display:none; padding:0; }
  /* Tiny expand button that lives next to clear in the header */
  .show-hints-btn { background:transparent; border:1px solid #2f343c;
                    color:#666; padding:3px 10px; border-radius:12px;
                    font-size:11px; cursor:pointer; }
  .show-hints-btn:hover { color:#a0c4ff; border-color:#a0c4ff; }
  /* v2.8 progressive help overlay — appears after 3 consecutive clarify */
  #help-overlay { position:fixed; bottom:80px; right:24px; max-width:340px;
                  background:#262a31; border:1px solid #50e3c2;
                  border-left-width:3px; border-radius:6px;
                  padding:14px 16px; color:#eaeaea; font-size:13px;
                  box-shadow:0 4px 16px rgba(0,0,0,0.4); z-index:50;
                  display:none; }
  #help-overlay h4 { margin:0 0 8px 0; color:#50e3c2; font-size:14px; }
  #help-overlay .ex { color:#a0c4ff; font-family:ui-monospace,monospace;
                       font-size:12px; padding:2px 0; cursor:pointer; }
  #help-overlay .ex:hover { color:#eaeaea; }
  #help-overlay .close { position:absolute; top:6px; right:10px;
                         color:#666; cursor:pointer; font-size:18px; }
  #help-overlay .close:hover { color:#eaeaea; }
  /* v2.5 onboarding overlay (first-visit only) */
  #onboarding-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.8);
                        z-index:100; display:flex; align-items:center;
                        justify-content:center; }
  #onboarding-card { background:#1c1f24; border:1px solid #2f343c;
                     border-radius:8px; padding:28px 32px; max-width:560px;
                     box-shadow:0 8px 32px rgba(0,0,0,0.5); }
  #onboarding-card h2 { margin:0 0 16px 0; color:#50e3c2; font-size:18px; }
  #onboarding-card p { margin:8px 0; color:#d0d0d0; font-size:14px;
                       line-height:1.5; }
  #onboarding-card .feat { margin:10px 0; padding-left:0; color:#a0c4ff;
                           font-size:13px; }
  #onboarding-card .feat::before { content:"✓ "; color:#7ed321;
                                    margin-right:4px; }
  #onboarding-card .actions { margin-top:20px; display:flex; gap:10px;
                              justify-content:flex-end; }
  #onboarding-card button { background:#50e3c2; color:#0a0c10;
                            font-weight:600; padding:8px 20px;
                            border:0; border-radius:4px; cursor:pointer; }
  #onboarding-card button:hover { background:#7ee3c8; }
</style>
</head>
<body>
<div id=onboarding-overlay style="display:none;">
  <div id=onboarding-card>
    <h2>Привет, я Словоёб</h2>
    <p>Литературный аналитик корпуса Project Gutenberg — <b>~55 000 английских книг</b>, проиндексированных семантически (ChromaDB) и лексически (FTS5).</p>
    <p>Что умею:</p>
    <div class=feat>Стилометрия: фирменные слова автора, сравнение, кто на кого похож</div>
    <div class=feat>Книги: уровень сложности, архаизмы, эмоциональный профиль</div>
    <div class=feat>Слова: контексты, collocates, этимология (Wiktionary), эпохи</div>
    <div class=feat>Лексика для изучения: B1/B2/C1, экспорт в Anki</div>
    <div class=feat>Топ-листы: по странам, скачиваниям, токенам</div>
    <p style="margin-top:14px;">Спрашивай по-русски или по-английски. Можно начать с подсказок ниже — они кликабельны.</p>
    <div class=actions>
      <button onclick="dismissOnboarding()">поехали</button>
    </div>
  </div>
</div>
<header>
  <h1>__ASSISTANT_NAME__ · wordcracker</h1>
  <span class=meta>v2 engine · wordcracker:v2 · planner→router→renderer→critic</span>
  <span style="flex:1"></span>
  <button class="show-hints-btn" type=button onclick="toggleHints()"
          title="показать примеры запросов">💡 примеры</button>
  <button class=secondary type=button onclick="clearHistory()">clear</button>
</header>
<div id=log></div>
<div id=suggestions></div>
<div id=help-overlay>
  <span class=close onclick="dismissHelp()">×</span>
  <h4>Не понимаю. Попробуй так:</h4>
  <div class=ex onclick="useExample('фирменные слова Doyle')">фирменные слова Doyle</div>
  <div class=ex onclick="useExample('уровень сложности Pride and Prejudice')">уровень сложности Pride and Prejudice</div>
  <div class=ex onclick="useExample('что у тебя с копирайтом')">что у тебя с копирайтом</div>
  <div class=ex onclick="useExample('этимология слова sword')">этимология слова sword</div>
</div>
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
// v2.9: client-side history clip. Stan round 5 Q11 caught localStorage
// overflow → HTTP 400 once total payload (history + new question) >64KB
// (the server-side cap from v2.3). 200 KB total cap here, with
// truncate-from-head when exceeded, guarantees no request ever hits the
// server-side limit. Plus 30-turn cap (which already matches the server's
// MAX_HISTORY_TURNS).
const HIST_MAX_BYTES = 200 * 1024;
const HIST_MAX_TURNS = 30;

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HKEY)) || []; } catch { return []; }
}
function _serialize(h) { return JSON.stringify(h); }
function saveHistory(h) {
  // Clip from the oldest end if the serialized history exceeds caps.
  // Keep the most recent turns — they're what the planner / followup
  // inference actually reads.
  let clipped = h;
  if (clipped.length > HIST_MAX_TURNS) {
    clipped = clipped.slice(-HIST_MAX_TURNS);
  }
  let serialized = _serialize(clipped);
  while (serialized.length > HIST_MAX_BYTES && clipped.length > 2) {
    clipped = clipped.slice(2);  // drop oldest user+assistant pair
    serialized = _serialize(clipped);
  }
  try {
    localStorage.setItem(HKEY, serialized);
  } catch (e) {
    // QuotaExceededError or similar — fall back to keeping only last 4 turns
    try {
      localStorage.setItem(HKEY, _serialize(clipped.slice(-4)));
    } catch {
      localStorage.removeItem(HKEY);
    }
  }
}
function clearHistory() {
  if (confirm('Очистить историю?')) {
    localStorage.removeItem(HKEY);
    log.innerHTML = '';
    document.getElementById('suggestions').classList.remove('force-show');
    renderSuggestions();
  }
}
// Sprint 18 — manual toggle so user can re-show example chips after the
// CSS-driven auto-hide kicks in. Force-show wins over the
// `#log:not(:empty) ~ #suggestions { display:none }` rule. Also
// re-renders chips if the container was emptied by renderSuggestions
// (history-non-empty branch — chips need re-creating).
function toggleHints() {
  const host = document.getElementById('suggestions');
  if (host.classList.contains('force-show')) {
    host.classList.remove('force-show');
    return;
  }
  if (host.children.length === 0) {
    // Re-populate; render uses history-empty shortcut so bypass it
    const saved = window._forceRender || false;
    window._forceRender = true;
    renderSuggestions();
    window._forceRender = saved;
  }
  host.classList.add('force-show');
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

function renderError(msg, retryText) {
  const div = document.createElement('div');
  div.className = 'msg error';
  const txt = document.createElement('div');
  txt.textContent = msg;
  div.appendChild(txt);
  // v2.5 demo polish: errors get a retry button. Same query, fresh request.
  if (retryText) {
    const retry = document.createElement('button');
    retry.className = 'retry-scope';
    retry.type = 'button';
    retry.textContent = '↻ повторить';
    retry.onclick = () => { submit(retryText); };
    div.appendChild(retry);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function submit(text) {
  hideSuggestionsOnSubmit();
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
  let intentLabel = null;        // captured from {event:'intent'} early in stream
  let intentConfidence = null;   // confidence value from same event
  let criticInfo  = null;        // captured from {event:'critic'} late in stream

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
            intentConfidence = ev.confidence;
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
            renderError(ev.message, text);
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

      // Sprint 22+ — feedback button «🚩 неправильный». Click flags
      // this assistant turn to /api/flag_bad_answer with full context
      // (question, answer, intent, tool_calls, elapsed_sec). Optional
      // user note via prompt. Admin reviews at /admin/bad_answers.
      const flagBtn = document.createElement('button');
      flagBtn.className = 'flag-btn'; flagBtn.type = 'button';
      flagBtn.textContent = '🚩 неправильный';
      flagBtn.title = 'Пометить ответ как кривой — отправляется админу для разбора';
      flagBtn.onclick = async () => {
        const note = window.prompt(
            'Что не так с ответом? (опционально, можно оставить пустым)',
            '');
        if (note === null) return;  // user cancelled
        flagBtn.disabled = true;
        flagBtn.textContent = '⏳ отправляю...';
        try {
          const r = await fetch('/api/flag_bad_answer', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              question: text,
              answer: answerText,
              intent: intentLabel,
              intent_confidence: intentConfidence,
              tool_calls: final ? (final.tool_calls || tools) : tools,
              elapsed_sec: final ? final.elapsed_sec : null,
              render_meta: final ? (final.render_meta || null) : null,
              critic_summary: criticInfo ? criticInfo.summary : null,
              user_note: note,
              history: history.slice(-10),
            }),
          });
          if (r.ok) {
            const body = await r.json().catch(() => ({}));
            flagBtn.textContent = '✓ записано (' + (body.id || 'ok') + ')';
            flagBtn.classList.add('flag-btn-sent');
          } else {
            flagBtn.textContent = '✗ ошибка ' + r.status;
            flagBtn.disabled = false;
          }
        } catch (err) {
          flagBtn.textContent = '✗ сеть';
          flagBtn.disabled = false;
        }
      };
      badges.appendChild(flagBtn);
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
      // v2.8: count consecutive clarifies; show help overlay after 3.
      bumpClarify();
    } else {
      // Successful response — reset the clarify streak counter.
      resetClarifyStreak();
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
    // v2.5: refresh the footer immediately so user sees the new
    // count/avg without waiting for the next 10s tick.
    refreshStats();
  } catch (e) {
    clearInterval(timer);
    live.remove();
    renderError('Stream error: ' + e.message, text);
  } finally {
    send.disabled = false;
    send.textContent = 'send';
    q.focus();
  }
}

// v2.5 demo polish: render clickable suggestion chips so a first-time
// user sees what they CAN ask, not «здесь была подсказка тонким серым».
// Hidden as soon as the conversation starts (first message in log).
const SUGGESTIONS = [
  { cat: 'Стиль автора', items: [
    'фирменные слова Оскара Уайльда',
    'сравни По и Лавкрафта по стилю',
    'на кого по стилю похож Doyle',
  ]},
  { cat: 'Книги', items: [
    'уровень сложности Pride and Prejudice',
    'архаизмы в Dracula',
    'характерные прилагательные в "The Picture of Dorian Gray"',
  ]},
  { cat: 'Слова', items: [
    'этимология слова sword',
    'что соседствует со словом fog у викторианцев',
    'примеры использования слова "ajar"',
  ]},
  { cat: 'Корпус', items: [
    'сколько книг в базе',
    'что у тебя с копирайтом',
    'топ-5 британских авторов по скачиваниям',
  ]},
];
function renderSuggestions() {
  const host = document.getElementById('suggestions');
  // Sprint 18 — force-show bypasses the «empty when history non-empty»
  // guard, so the manual «💡 примеры» button can repopulate chips.
  if (!window._forceRender && loadHistory().length > 0) {
    host.innerHTML = ''; return;
  }
  host.innerHTML = '';
  for (const group of SUGGESTIONS) {
    const wrapper = document.createElement('div');
    wrapper.style.cssText = 'width:100%; display:flex; flex-wrap:wrap; gap:8px; align-items:baseline;';
    const h = document.createElement('h3'); h.textContent = group.cat;
    wrapper.appendChild(h);
    for (const item of group.items) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = item;
      chip.onclick = () => {
        q.value = item;
        q.focus();
        document.getElementById('f').requestSubmit();
      };
      wrapper.appendChild(chip);
    }
    host.appendChild(wrapper);
  }
}
function hideSuggestionsOnSubmit() {
  // v2.8 — keep chips visible but collapse them so they don't crowd the
  // log. Click on the «💡 примеры запросов» label to expand again.
  const host = document.getElementById('suggestions');
  if (!host.classList.contains('collapsed') && host.children.length > 0) {
    host.classList.add('collapsed');
    host.onclick = (ev) => {
      // Only toggle when clicking the empty label area, not a chip
      if (ev.target === host) {
        host.classList.toggle('collapsed');
      }
    };
  }
}

// v2.8: progressive help overlay after N consecutive clarify responses.
const CLARIFY_KEY = 'wordcracker_clarify_streak';
function bumpClarify() {
  const n = (parseInt(localStorage.getItem(CLARIFY_KEY) || '0', 10) + 1);
  localStorage.setItem(CLARIFY_KEY, String(n));
  if (n >= 3) {
    document.getElementById('help-overlay').style.display = 'block';
  }
}
function resetClarifyStreak() {
  localStorage.setItem(CLARIFY_KEY, '0');
  document.getElementById('help-overlay').style.display = 'none';
}
function dismissHelp() {
  document.getElementById('help-overlay').style.display = 'none';
  localStorage.setItem(CLARIFY_KEY, '0');
}
function useExample(text) {
  dismissHelp();
  q.value = text;
  document.getElementById('f').requestSubmit();
}

// Sprint 11.5 / v2.5: poll /api/stats every 10s so the sticky footer shows
// live counters from the in-process ring buffer. v2.5 cut the interval
// from 30s → 10s + added an explicit refresh after each user submit,
// since Stan's 2026-05-18 demon round noticed the counters looked frozen
// during fast-paced sessions. Failures are silent — footer just keeps
// the last successful values.
async function refreshStats() {
  try {
    const r = await fetch('/api/stats', {cache: 'no-store'});
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
setInterval(refreshStats, 10000);

// v2.5 onboarding: show first-visit splash. Suppress on returning users
// (localStorage flag set on dismiss).
const ONB_KEY = 'wordcracker_onboarded_v1';
function dismissOnboarding() {
  document.getElementById('onboarding-overlay').style.display = 'none';
  localStorage.setItem(ONB_KEY, '1');
  q.focus();
}
if (!localStorage.getItem(ONB_KEY) && loadHistory().length === 0) {
  document.getElementById('onboarding-overlay').style.display = 'flex';
}

// restore history
for (const m of loadHistory()) {
  render(m.role, m.content);
}
renderSuggestions();

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
            #
            # Hardening (v2.3.2): emit ONLY the counters the footer needs.
            # `aggregate_recent()` also returns an `intents` histogram and a
            # `slow_tools` list — both useful for the status dashboard but
            # they leak user query patterns and which tools are heavy enough
            # to DoS, which is reconnaissance for an attacker who slipped
            # past nginx Basic Auth.
            try:
                from scripts.v2.observability import aggregate_recent
                full = aggregate_recent()
                payload = {
                    "total":           full.get("total", 0),
                    "avg_elapsed_ms":  full.get("avg_elapsed_ms", 0),
                    "cache_hit_rate":  full.get("cache_hit_rate", 0.0),
                    "cache_hits":      full.get("cache_hits", 0),
                    "cache_calls":     full.get("cache_calls", 0),
                    "critic_flagged":  full.get("critic_flagged", 0),
                }
            except ImportError:
                payload = {"total": 0, "engine": "v1"}
            return self._send(200, json.dumps(payload, ensure_ascii=False, default=str),
                              "application/json; charset=utf-8")
        # Sprint 22+ — admin: list flagged bad answers as JSON.
        # Behind same Basic Auth as the rest. JSONL on disk so ops can
        # also grep / tail directly from the host.
        if self.path == "/admin/bad_answers" or self.path.startswith(
                "/admin/bad_answers?"):
            try:
                from scripts.v2.feedback import list_recent
            except ImportError as e:
                return self._send(500,
                                  json.dumps({"error": f"feedback unavailable: {e}"}),
                                  "application/json")
            # Parse query string ?limit=N&days=N
            limit = 200
            days = 7
            if "?" in self.path:
                qs = self.path.split("?", 1)[1]
                from urllib.parse import parse_qs
                params = parse_qs(qs)
                try:
                    limit = max(1, min(int(params.get("limit", ["200"])[0]),
                                        1000))
                except (ValueError, IndexError):
                    pass
                try:
                    days = max(1, min(int(params.get("days", ["7"])[0]), 90))
                except (ValueError, IndexError):
                    pass
            try:
                records = list_recent(days_back=days, limit=limit)
            except Exception as e:
                return self._send(500,
                                  json.dumps({"error": str(e)}),
                                  "application/json")
            return self._send(200,
                              json.dumps({"count": len(records),
                                          "records": records},
                                         ensure_ascii=False, default=str,
                                         indent=2),
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
        # Sprint 22+ alpha6 (Round 13 B1) — SSE keep-alive heartbeat.
        # Long tool calls (~60-120s for heavy queries) used to silently
        # drop the connection because no event fired during the wait,
        # and nginx/CF closed the idle stream. Wrap the generator in
        # a queue+thread: main loop pulls with 20s timeout and writes
        # SSE comment `:keepalive\n\n` on timeout. Comments don't
        # trigger client `onmessage` so the UI stays clean.
        import queue, threading
        ev_q: queue.Queue = queue.Queue()
        SENTINEL = object()

        def _producer():
            try:
                for ev in ask_stream(question, history=history, engine=engine):
                    ev_q.put(ev)
            except Exception as e:
                ev_q.put({"_producer_error": e})
            finally:
                ev_q.put(SENTINEL)

        threading.Thread(target=_producer, daemon=True).start()

        try:
            while True:
                try:
                    ev = ev_q.get(timeout=20)
                except queue.Empty:
                    # Keep connection alive while a tool runs > 20s.
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception:
                        return
                    continue
                if ev is SENTINEL:
                    break
                if isinstance(ev, dict) and "_producer_error" in ev:
                    raise ev["_producer_error"]
                line = "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
                try:
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return  # client gone — stop pumping events
                except Exception:
                    return  # client gone (TLS error, etc.)
        except Exception as e:
            # Sprint 21 B104 — soft fix for «network error». Top-level
            # SSE catch sometimes never makes it to the client (headers
            # already sent, connection half-closed). Emit a structured
            # friendly event + an answer-shaped fallback so the UI has
            # something to show regardless of where in the stream we
            # died. Each write is wrapped — by the time we get here
            # the connection may be broken.
            msg = str(e)[:200].replace('"', "'").replace("\n", " ")
            payload = (
                f'data: {{"event":"error","kind":"server",'
                f'"message":"⚠️ Сбой сервера ({type(e).__name__}). '
                f'Попробуй ещё раз через минуту.","detail":"{msg}"}}\n\n'
            )
            try:
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
            # Also emit an answer event with a fallback message so the
            # client doesn't get stuck on a spinner waiting for `answer`.
            try:
                fallback = (
                    'data: {"event":"answer",'
                    '"text":"⚠️ Сервер не смог ответить. '
                    'Попробуй переформулировать запрос или подожди 30 секунд."}\n\n'
                )
                self.wfile.write(fallback.encode("utf-8"))
                self.wfile.write(b'data: {"event":"done"}\n\n')
                self.wfile.flush()
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
        # Sprint 22+ — user feedback collection. Stan asked for a way
        # to flag bad answers from the chat UI so fixes can be
        # prioritized from real user reports instead of waiting for
        # external Claude rounds. Append-only JSONL at
        # /workspace/spgc/derived/v2_feedback/bad-YYYY-MM-DD.jsonl.
        if self.path == "/api/flag_bad_answer":
            payload, err = self._read_payload_capped()
            if err:
                return self._send(400, json.dumps({"error": err}).encode("utf-8"),
                                  "application/json")
            try:
                from scripts.v2.feedback import record_bad_answer
            except ImportError as e:
                return self._send(500,
                                  json.dumps({"error": f"feedback unavailable: {e}"}),
                                  "application/json")
            try:
                ip = (self.headers.get("X-Forwarded-For")
                      or self.client_address[0])
                rec = record_bad_answer(
                    question=payload.get("question") or "",
                    answer=payload.get("answer") or "",
                    intent=payload.get("intent"),
                    intent_confidence=payload.get("intent_confidence"),
                    tool_calls=payload.get("tool_calls") or [],
                    elapsed_sec=payload.get("elapsed_sec"),
                    render_meta=payload.get("render_meta"),
                    critic_summary=payload.get("critic_summary"),
                    user_note=payload.get("user_note"),
                    history=payload.get("history") or [],
                    ip=ip,
                )
            except ValueError as e:
                return self._send(400,
                                  json.dumps({"error": str(e)}),
                                  "application/json")
            except Exception as e:
                return self._send(500,
                                  json.dumps({"error": f"flag failed: {e}"}),
                                  "application/json")
            return self._send(200,
                              json.dumps({"ok": True, "id": rec["id"]},
                                         ensure_ascii=False),
                              "application/json; charset=utf-8")
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
    # v2.5 demo polish: pre-run the cheap meta queries so the first user
    # query that hits them isn't paying cold-cache latency. These all use
    # WC_DEFAULT_ENGINE=v2 path and warm the dispatch + cache layers.
    try:
        from scripts.v2.tool_registry import dispatch
        t = _time.perf_counter()
        # corpus_overview is the typical first-time-user opener
        dispatch("corpus_overview", {})
        # top authors (books / downloads / tokens — all three cached separately)
        dispatch("top_authors_by", {"metric": "books", "top": 10})
        dispatch("top_authors_by", {"metric": "downloads", "top": 10})
        dispatch("top_authors_by", {"metric": "tokens", "top": 10})
        # one popular author_metadata to warm the parquet read + geo lookup
        dispatch("author_metadata", {"author_regex": "^Doyle,"})
        print(f"[chat] v2 dispatch warmed in {_time.perf_counter()-t:.1f}s "
              f"(corpus_overview + 3 top_authors variants + Doyle metadata)",
              file=_sys.stderr, flush=True)
    except Exception as e:
        print(f"[chat] v2 dispatch warmup failed (non-fatal): "
              f"{type(e).__name__}: {e}", file=_sys.stderr, flush=True)


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
