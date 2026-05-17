"""Functional test: hit /api/chat with each of the 40 Примеры via v2 engine.

Outputs a markdown report grouped by intent + verdict.

Run on the host (server-on-wheels):
    python3 ~/wordcracker/tests/v2/run_functional_40.py \
      --base-url http://127.0.0.1:8890 \
      --engine v2 \
      --out test_report_v2_$(date +%Y-%m-%d).md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


QUESTIONS_40 = [
    "Напиши, что ты умеешь, какие типы анализа поддерживаешь, и приведи пример сложного исследовательского запроса.",
    "Какие слова у Конан Дойла встречаются заметно чаще, чем у остальных английских авторов XIX века?",
    "Покажи мне не слишком редкие, но характерные слова Толкина, которые обычно не знают изучающие английский.",
    "Какие слова чаще всего вызывают сложности у читателей уровня B2 при чтении Лавкрафта?",
    "Найди слова, которые постоянно повторяются у Диккенса, но почти не встречаются у Хемингуэя.",
    "Какие необычные британские слова часто использует Агата Кристи?",
    "Покажи слова, которые в книге «Преступление и наказание» используются намного чаще, чем в среднем по библиотеке.",
    "Какие слова у Толкина имеют древнегерманское или скандинавское происхождение?",
    'Какие слова чаще всего соседствуют со словом "fog" у викторианских авторов?',
    "Покажи мне лексику «второго уровня» из этой книги — не базовые слова, но и не совсем экзотику.",
    'Какие слова из "Dracula" сейчас считаются устаревшими или архаичными?',
    "Найди слова, которые в американской литературе используются редко, а в британской — часто.",
    "Какие характерные прилагательные чаще всего использует Оскар Уайльд?",
    "Покажи слова, которые я, скорее всего, не знаю, если понимаю примерно 80% текста книги «1984».",
    "Какие слова сильнее всего отличают стиль По от стиля Лавкрафта?",
    'Покажи примеры использования слова "ajar" у разных авторов и объясни оттенки значения.',
    "Какие слова резко вышли из употребления после 1920 года?",
    "Найди слова, которые почти всегда используются в мрачном или тревожном контексте.",
    "Какие слова в этой книге имеют больше всего разных значений в зависимости от контекста?",
    "Какие слова чаще всего переводят неправильно или упрощают в русских переводах викторианской литературы?",
    "Если бы я хотел читать Голсуорси свободно, какие 300 слов мне нужно выучить в первую очередь?",
    "Какие слова характерны для английских текстов, опубликованных до 1900 года, но почти исчезают после 1900?",
    "Сравни лексику британских и американских авторов XIX века: какие слова дают самый сильный перекос?",
    "Какие слова чаще всего встречаются в приключенческой литературе, но редко встречаются в романах воспитания?",
    "Покажи 100 слов, которые отличают готическую прозу от реалистического романа XIX века.",
    "Какие авторы лексически ближе всего к Конан Дойлу?",
    "Найди слова, которые часто встречаются у морских авторов — Мелвилла, Конрада и Стивенсона — но редко в остальном корпусе.",
    "Какие слова у Джейн Остин выглядят обычными сейчас, но в её текстах используются в необычных контекстах?",
    "Покажи слова, которые в русских переводах чаще всего соответствуют нескольким разным английским словам.",
    "Какие произведения подойдут для читателя уровня B2: не слишком простые, но без плотного слоя архаизмов?",
    'Построй "словарный паспорт" автора: 50 характерных слов, 20 любимых прилагательных, 20 частых глаголов, 20 архаизмов и 10 слов с интересной этимологией.',
    "Покажи слова, которые были популярны у викторианских авторов, но почти исчезли в современной литературе.",
    "Какие слова чаще всего используются в описаниях тумана, дождя и сырой погоды?",
    "Найди авторов с самым богатым словарём по количеству уникальных лемм.",
    'Какие слова чаще всего встречаются рядом со словами "fear", "terror" и "madness"?',
    "Какие авторы используют больше всего редких прилагательных?",
    "Найди слова, которые почти всегда встречаются в диалогах, а не в авторском тексте.",
    "Какие слова наиболее характерны для женских персонажей викторианской литературы?",
    "Покажи самые необычные глаголы движения в английской литературе XIX века.",
    "Возьми все английские произведения 1850–1920 годов, раздели их на британских и американских авторов, "
    "убери 1000 самых частотных слов, сгруппируй слова по леммам и частям речи, а затем покажи 200 слов уровня B2–C1, "
    "которые сильнее всего отличают британскую прозу от американской.",
]


def ask_one(base_url: str, q: str, engine: str, timeout: int) -> dict:
    payload = json.dumps({"question": q, "engine": engine}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        elapsed = time.perf_counter() - t0
        return {**json.loads(body), "_elapsed_wall": elapsed, "_ok": True}
    except urllib.error.HTTPError as e:
        return {"_ok": False, "_elapsed_wall": time.perf_counter() - t0,
                "_error": f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}"}
    except urllib.error.URLError as e:
        return {"_ok": False, "_elapsed_wall": time.perf_counter() - t0,
                "_error": f"URLError: {e.reason}"}
    except Exception as e:
        return {"_ok": False, "_elapsed_wall": time.perf_counter() - t0,
                "_error": f"{type(e).__name__}: {e}"}


def verdict(res: dict) -> str:
    if not res.get("_ok"):
        return "fail"
    if res.get("_elapsed_wall", 0) > 170:
        return "timeout"
    ans = (res.get("answer") or "").strip()
    if not ans:
        return "empty"
    intent = (res.get("intent") or "").lower()
    if intent in ("clarify",):
        return "clarify"
    if intent in ("out_of_scope", "translation_quality"):
        return "out_of_scope"
    if not res.get("tool_calls"):
        # Introductions answer without tools — that's fine
        if intent == "introduction":
            return "pass-no-tool"
        return "pass-no-tool"  # may still be legitimate
    # Check tools succeeded
    ok_calls = [tc for tc in res["tool_calls"] if tc.get("ok", True)]
    if not ok_calls:
        return "partial"
    return "pass"


def run(base_url: str, engine: str, timeout: int, qs: list[str]) -> list[dict]:
    out = []
    for i, q in enumerate(qs, 1):
        print(f"[{i:02d}/{len(qs)}] {q[:70]}...", flush=True)
        r = ask_one(base_url, q, engine, timeout)
        out.append({"qid": i, "question": q, "result": r, "verdict": verdict(r)})
        print(f"     → {out[-1]['verdict']:12s} "
              f"intent={r.get('intent', '?'):25s} "
              f"tools={[tc['name'] for tc in r.get('tool_calls', [])]} "
              f"{r.get('_elapsed_wall', 0):.1f}s", flush=True)
    return out


def write_report(rows: list[dict], out_path: Path, engine: str, base_url: str):
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    md = [
        f"# Functional Test Report — v2 engine ({engine})",
        "",
        f"Run date: {datetime.now().isoformat(timespec='seconds')}",
        f"Target: {base_url}",
        f"Total queries: {len(rows)}",
        "",
        "## Summary",
        "",
        "| Verdict | Count | % |",
        "|---|---:|---:|",
    ]
    for v in sorted(counts):
        md.append(f"| {v} | {counts[v]} | {counts[v]/len(rows):.0%} |")
    md.append("")
    md.append("## Per-question")
    md.append("")
    md.append("| QID | Verdict | Intent | Tools | Time | Note |")
    md.append("|---|---|---|---|---:|---|")

    for r in rows:
        res = r["result"]
        tools = ", ".join(tc["name"] for tc in res.get("tool_calls", [])) or "—"
        note = ""
        if not res.get("_ok"):
            note = res.get("_error", "")[:60]
        md.append(f"| Q{r['qid']:02d} | {r['verdict']} | "
                  f"{res.get('intent', '?')} | {tools} | "
                  f"{res.get('_elapsed_wall', 0):.1f}s | {note} |")

    md.append("")
    md.append("## Full Answers")
    for r in rows:
        res = r["result"]
        md.append("")
        md.append(f"### Q{r['qid']:02d} — {r['verdict']}")
        md.append(f"**Q:** {r['question']}")
        md.append("")
        md.append(f"- Intent: `{res.get('intent', '?')}`"
                  f" (conf={res.get('intent_confidence', 0):.2f})")
        md.append(f"- Tools: `{[tc['name'] for tc in res.get('tool_calls', [])]}`")
        md.append(f"- Wall time: {res.get('_elapsed_wall', 0):.1f}s")
        if not res.get("_ok"):
            md.append(f"- Error: {res.get('_error')}")
        ans = (res.get("answer") or "")[:2000]
        if ans:
            md.append("")
            md.append("**Answer:**")
            md.append("")
            md.append(ans)
        md.append("")
        md.append("---")

    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"\nReport written to {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8890")
    ap.add_argument("--engine", default="v2", choices=("v1", "v2"))
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--out", default=None,
                    help="markdown report path (default: test_report_v2_YYYY-MM-DD.md)")
    ap.add_argument("--only", help="comma-separated qids to run (e.g. 2,5,11)")
    args = ap.parse_args()

    if args.only:
        ids = {int(x) for x in args.only.split(",")}
        qs = [QUESTIONS_40[i - 1] for i in sorted(ids) if 1 <= i <= len(QUESTIONS_40)]
    else:
        qs = QUESTIONS_40

    rows = run(args.base_url, args.engine, args.timeout, qs)

    out = (Path(args.out) if args.out
           else Path(f"test_report_{args.engine}_{datetime.now():%Y-%m-%d}.md"))
    write_report(rows, out, args.engine, args.base_url)

    # Exit code: 0 if ≥ 70% pass, 1 otherwise.
    pass_like = sum(1 for r in rows
                    if r["verdict"] in ("pass", "pass-no-tool", "out_of_scope"))
    rate = pass_like / len(rows) if rows else 0
    print(f"\nPass rate (pass + pass-no-tool + out_of_scope): "
          f"{pass_like}/{len(rows)} = {rate:.0%}")
    sys.exit(0 if rate >= 0.7 else 1)


if __name__ == "__main__":
    main()
