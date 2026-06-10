"""S6 webapp test fixtures — everything stubbed, no Ollama/Chroma/GPU.

The suite must be green on a bare CI runner: rag_v2.ask_stream is
monkeypatched per-test, the FastAPI app is exercised via TestClient,
and xlsx assertions read the workbook back with openpyxl.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo root on sys.path so `scripts.*` / `api.*` import regardless of
# pytest invocation directory (mirrors how tests/v2 relies on rootdir).
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
