"""Quick hybrid_search sanity — exercises the live FTS5 + ChromaDB RRF pipeline.

Run on the host:
    python3 ~/wordcracker/tests/v2/sanity_hybrid.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8890"

QUERIES = [
    'Покажи примеры использования слова "ajar" у разных авторов',
    'Где упоминается "consumption" в литературе XIX века',
]


def ask(q: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=json.dumps({"question": q}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main() -> int:
    fails = 0
    for q in QUERIES:
        print(f"--- {q}")
        try:
            d = ask(q)
        except Exception as e:
            print(f"  ERROR: {e}")
            fails += 1
            continue
        intent = d.get("intent", "?")
        tools = [tc["name"] for tc in d.get("tool_calls", [])]
        elapsed = d.get("elapsed_sec")
        answer = (d.get("answer") or "")[:240].replace("\n", " ")
        print(f"  intent={intent} tools={tools} elapsed={elapsed}s")
        print(f"  answer={answer}")
        if "hybrid_search" not in tools:
            print(f"  WARN: expected hybrid_search in tool chain, got {tools}")
    return fails


if __name__ == "__main__":
    sys.exit(main())
