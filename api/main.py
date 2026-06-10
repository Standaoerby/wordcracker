"""wordcracker-api — FastAPI service for the S6 web UI.

Separate service from chat_server (:8890): same image/venv, own container
(`wordcracker-api`, :8000). Контракт и SSE-протокол — docs/webapp.md.

Key rules (plan.md §3, CLAUDE.md «Web app»):
  - SSE via EventSourceResponse over a SYNC generator — FastAPI runs it in
    the threadpool, the event loop stays free;
  - NO GZipMiddleware on the SSE route (it breaks streaming);
  - /api/health is pure liveness (docker healthcheck target) — it must NOT
    probe Ollama, or an Ollama restart drags the api into a restart loop;
    /api/ready is the readiness probe (Ollama + Chroma), for operators;
  - api routes are registered BEFORE the static mount on "/";
    no SPA catch-all in S6 (single screen, no client router).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import requests as _requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# fastapi>=0.135 ships fastapi.sse; older pins fall back to sse-starlette.
# Which one won is recorded in decisions.md D17 and surfaced on
# /api/health for operators. NB the two APIs differ: fastapi.sse encodes
# in the routing layer (we pre-format frames via format_sse_event and
# stream bytes), sse-starlette consumes ServerSentEvent objects.
try:
    from fastapi.sse import EventSourceResponse, format_sse_event  # type: ignore
    SSE_IMPL = "fastapi.sse"

    def _sse_response(frames):
        """frames: iterable of (event_name, json_payload_str)."""
        def gen():
            for event, payload in frames:
                yield format_sse_event(event=event, data_str=payload)
        return EventSourceResponse(gen())
except ImportError:  # pragma: no cover - depends on installed fastapi
    from sse_starlette.sse import EventSourceResponse, ServerSentEvent
    SSE_IMPL = "sse-starlette"

    def _sse_response(frames):
        def gen():
            for event, payload in frames:
                yield ServerSentEvent(event=event, data=payload)
        return EventSourceResponse(gen())

from scripts.api_loop import stream_answer
from scripts.v2.__version__ import ANALYTICS_VERSION
from api.export import safe_filename, tables_to_xlsx

log = logging.getLogger("wordcracker.api")

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "WC_API_CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
WEB_DIST = Path(os.environ.get("WC_WEB_DIST", "web/dist"))

# ---------- warmup ----------

_warm = {"embedder": False}


def _warmup() -> None:
    """Pre-load ChromaDB + the query embedder (CUDA, CPU-fallback — see
    rag_tools._resolve_embedder_device) so the first user query doesn't
    pay the ~30s cold cost. Mirrors chat_server's startup warmup."""
    try:
        from scripts import rag_tools
        rag_tools._get_chroma_collection_with_embedder()
        _warm["embedder"] = True
        log.info("warmup: chroma collection + query embedder ready")
    except Exception:
        log.exception("warmup failed — first query pays the cold cost")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    threading.Thread(target=_warmup, daemon=True, name="api-warmup").start()
    yield


app = FastAPI(title="wordcracker-api", version=ANALYTICS_VERSION,
              lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- health / ready ----------

@app.get("/api/health")
def health() -> dict:
    """Pure liveness — docker healthcheck. Never probes Ollama (Q3)."""
    return {"ok": True, "version": ANALYTICS_VERSION, "sse": SSE_IMPL}


@app.get("/api/ready")
def ready():
    """Readiness: Ollama reachable + embedder/Chroma warm. 503 until both.
    NOT wired into the docker healthcheck (restart-loop hazard, Q3)."""
    ollama_ok = False
    try:
        ollama_ok = _requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2).ok
    except Exception:
        pass
    body = {"ready": bool(ollama_ok and _warm["embedder"]),
            "ollama": ollama_ok, "embedder": _warm["embedder"]}
    return JSONResponse(body, status_code=200 if body["ready"] else 503)


# ---------- query (SSE) ----------

class QueryBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    data_only: bool = False
    # Accepted but ignored until S9 (#4) — documented in webapp.md.
    history: list[dict] | None = None


@app.post("/api/query")
def query(body: QueryBody):
    def frames():
        for ev in stream_answer(body.question, data_only=body.data_only):
            # default=str — last-resort safety net only; table cells are
            # already native scalars (N1, table_extract._scalar).
            yield ev["event"], json.dumps(ev["data"], ensure_ascii=False,
                                          default=str)
    return _sse_response(frames())


# ---------- xlsx export ----------

class ExportBody(BaseModel):
    tables: list[dict]
    filename: str | None = None


@app.post("/api/export/xlsx")
def export_xlsx(body: ExportBody):
    content = tables_to_xlsx(body.tables)
    fname = safe_filename(body.filename)
    return Response(
        content=content,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------- static frontend (registered LAST — after all /api routes) ----------

if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True),
              name="web")
else:  # dev: Vite dev server on :5173 serves the UI, CORS points here
    @app.get("/")
    def index_stub() -> dict:
        return {"service": "wordcracker-api", "ui": "not built",
                "hint": "npm run dev in web/ (Vite :5173) or build web/dist"}
