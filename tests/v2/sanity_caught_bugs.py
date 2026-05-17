"""Smoke probes for the 3 «Отловленные баги» fixes (bugs #3, #4, #5).

Run on the host:
    python3 ~/wordcracker/tests/v2/sanity_caught_bugs.py
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8890"


def ask(question: str, history=None) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=json.dumps({"question": question,
                         "history": history or []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main() -> int:
    fails = 0

    # ----- Bug #3: Свифт + author_top_words -----
    print("\n=== Bug #3: Свифт alias + author_top_words intent ===")
    for q in ["давай самое частотное слово Джонатана Свифта",
              "фирменные слова Свифта"]:
        print(f"--- Q: {q}")
        d = ask(q)
        intent = d.get("intent", "?")
        tools = [tc["name"] for tc in d.get("tool_calls", [])]
        elapsed = d.get("elapsed_sec", 0)
        print(f"  intent={intent} tools={tools} elapsed={elapsed}s")
        ans = (d.get("answer") or "")[:150].replace("\n", " ")
        print(f"  answer={ans}")
        if intent == "clarify":
            print("  FAIL: still clarifying")
            fails += 1

    # ----- Bug #4: copyright friendly refusal -----
    print("\n=== Bug #4: copyright graceful OOS ===")
    for q in ['Какой уровень сложности у «1984» Оруэлла?',
              'Слова Толкина из “The Lord of the Rings”',
              'Архаизмы в “The Old Man and the Sea” Хемингуэя']:
        print(f"--- Q: {q}")
        d = ask(q)
        intent = d.get("intent", "?")
        tools = [tc["name"] for tc in d.get("tool_calls", [])]
        ans = (d.get("answer") or "")[:300].replace("\n", " ")
        print(f"  intent={intent} tools={tools}")
        print(f"  answer={ans}")
        if intent != "out_of_scope":
            print(f"  WARN: expected out_of_scope, got {intent}")

    # ----- Bug #5: multi-turn follow-up -----
    print("\n=== Bug #5: history-aware follow-up ===")
    # Simulate a 2-turn conversation: first ask about Wodehouse, then say
    # "give me three examples of these words".
    history = [
        {"role": "user", "content": "Покажи фирменные слова Wodehouse"},
        {"role": "assistant", "content": "wicket, blighter, hullo, chappie, bally"},
    ]
    q = "приведи три примера такого использования"
    print(f"--- Q: {q} (history → Wodehouse words)")
    d = ask(q, history=history)
    intent = d.get("intent", "?")
    tools = [tc["name"] for tc in d.get("tool_calls", [])]
    ans = (d.get("answer") or "")[:250].replace("\n", " ")
    print(f"  intent={intent} tools={tools}")
    print(f"  answer={ans}")
    if intent == "clarify":
        print("  FAIL: planner still asks clarify on follow-up")
        fails += 1

    print(f"\n=== {fails} hard fails ===")
    return fails


if __name__ == "__main__":
    sys.exit(main())
