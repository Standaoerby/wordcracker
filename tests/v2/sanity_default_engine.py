"""Quick sanity probes against the running chat_server using default engine.

After installing the WC_DEFAULT_ENGINE=v2 drop-in, request without specifying
?engine should still route through the v2 pipeline. The probes verify that
each query is classified by the v2 intent classifier (presence of an
`intent` field) and that the appropriate tools were called.

Run on the host:
    python3 tests/v2/sanity_default_engine.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8890"

PROBES = [
    ("кто ты", "introduction", []),
    ("сколько книг в базе", "corpus_meta", ["corpus_overview"]),
    ("фирменные слова Wodehouse", "author_vocab", ["affinity_by_author"]),
    ("какие архаизмы в Dracula", "book_archaic", ["book_archaic_words"]),
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
    for q, want_intent, want_tools in PROBES:
        print(f"--- {q}")
        try:
            d = ask(q)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            print(f"  ERROR: {e}")
            fails += 1
            continue
        intent = d.get("intent", "?")
        tools = [tc["name"] for tc in d.get("tool_calls", [])]
        elapsed = d.get("elapsed_sec")
        answer = (d.get("answer") or "")[:160].replace("\n", " ")
        print(f"  intent={intent} tools={tools} elapsed={elapsed}s")
        print(f"  answer={answer}")

        if "intent" not in d:
            print(f"  FAIL: no `intent` field — v1 engine still active?")
            fails += 1
            continue
        if intent != want_intent:
            print(f"  WARN: expected intent={want_intent}, got {intent}")
        if want_tools and not set(want_tools).issubset(set(tools)):
            print(f"  WARN: expected tools containing {want_tools}, got {tools}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
