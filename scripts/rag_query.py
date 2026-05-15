#!/usr/bin/env python3
"""
rag_query.py — agentic LLM entry point for wordcracker.

Pipeline:
  1. Send `messages` + TOOLS_SPEC to Ollama /api/chat
  2. While the response carries tool_calls (max max_iterations):
       - dispatch each tool_call via TOOL_DISPATCH (from rag_tools.py)
       - append tool result to messages as {"role": "tool", ...}
       - call /api/chat again
  3. Once the model produces a regular content answer, return it
     alongside the full tool-call trace.

Usage:
    from rag_query import ask
    r = ask("какие самые популярные биграммы у Wodehouse")
    print(r["answer"])
    for tc in r["tool_calls"]:
        print(tc["name"], tc["args"])

CLI:
    python rag_query.py "вопрос" [--model qwen3:14b] [--verbose]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rag_tools import TOOLS_SPEC as _BASE_TOOLS_SPEC, TOOL_DISPATCH as _BASE_DISPATCH
from learning_tools import LEARNING_TOOLS_SPEC, LEARNING_TOOL_DISPATCH

TOOLS_SPEC    = _BASE_TOOLS_SPEC + LEARNING_TOOLS_SPEC
TOOL_DISPATCH = {**_BASE_DISPATCH, **LEARNING_TOOL_DISPATCH}

DEFAULT_MODEL          = "qwen3:14b"
DEFAULT_OLLAMA_HOST    = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
DEFAULT_MAX_ITER       = 5
DEFAULT_TEMPERATURE    = 0.3
DEFAULT_KEEP_ALIVE     = "5m"  # keep model warm across the tool-call loop

SYSTEM_PROMPT = """Ты — литературный аналитик и помощник по корпусу Project Gutenberg.

У тебя есть набор инструментов для работы с корпусом. НИКОГДА не называй конкретные числа (количество книг, токенов, чанков) без вызова инструмента — корпус регулярно расширяется, твоя память устарела.

Выбирай правильный инструмент под вопрос пользователя:
- corpus_overview — «сколько книг в базе», «какой объём корпуса»
- semantic_search — «найди упоминания X в книгах», «где описывается Y»
- corpus_stats_by_author — «дай статистику по автору», «сколько у X книг»
- top_ngrams_by_author — «самые популярные биграммы/триграммы», «частые связки слов»
- affinity_by_author — «фирменные слова автора», «маркеры стиля», «характерная лексика»
- affinity_by_book — фирменные слова конкретной книги по PG id
- word_contexts — «в каком контексте слово X», «приведи примеры использования»
- compare_authors — «сравни автора A и автора B», «насколько похожи X и Y»

Для ИЗУЧЕНИЯ ЛЕКСИКИ (vocabulary learning):
- learning_words — «дай 100 слов из книги X для изучения», «какие слова стоит выучить у Doyle». Сам выбирает band (intermediate B1-B2 по умолчанию), отфильтровывает базовые/имена, лемматизирует.
- enrich_word — после learning_words: для каждого слова получить перевод, POS, definition, etymology, CEFR. Кешируется на диске.
- export_word_list — выгрузка в Anki CSV / Markdown.

Типичный vocabulary-flow:
  1) learning_words(scope={'book':'PG1342'}, level='intermediate', top=30)
     — результат уже содержит example_context для каждого слова
  2) для каждого слова: enrich_word(word, contexts=[results[i].example_context], lemma_hint, pos_hint)
     — ВСЕГДА передавай example_context из step 1, иначе LLM галлюцинирует на изолированных словах
     — если enrich_word вернул proper_noun=true, отбрось слово (имя/место)
  3) export_word_list(words=..., format='anki_csv')

Правила:
1. ВСЕГДА используй инструмент если вопрос про конкретные данные. Не выдумывай статистику и не цитируй устаревшие числа из памяти.
2. Цитаты подкрепляй ссылкой [Автор, "Название", PG12345].
3. Табличные данные форматируй как Markdown-таблицу.
4. После результата инструмента — пиши понятное summary, не сырой JSON.
5. Отвечай на языке вопроса пользователя.
6. Регексы для авторов: формат `^Surname,`. Примеры:
   - Достоевский → `^Dostoyevsky,`
   - Толстой → `^Tolstoy,`
   - Чехов → `^Chekhov,`
   - Wodehouse → `^Wodehouse,`
   - Doyle → `^Doyle,`
   ВАЖНО: основная часть корпуса — английская. Достоевский/Толстой/Чехов — в английских переводах.
7. Если tool вернул `error` — попробуй другой regex или сообщи пользователю что данных нет.
8. На «привет, что ты умеешь?» — отвечай напрямую без tools, перечисли категории возможностей.
"""


def _log(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"[rag] {msg}", file=sys.stderr)


def _truncate_for_summary(obj, max_chars: int = 200) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= max_chars else s[:max_chars] + "...(truncated)"


def _call_chat(messages: list, model: str, ollama_host: str,
               temperature: float, keep_alive: str) -> dict:
    payload = {
        "model":      model,
        "messages":   messages,
        "tools":      TOOLS_SPEC,
        "stream":     False,
        "keep_alive": keep_alive,
        "options":    {"temperature": temperature},
        "think":      False,
    }
    resp = requests.post(f"{ollama_host}/api/chat", json=payload, timeout=300)
    resp.raise_for_status()
    return resp.json()


def _execute_tool(name: str, args: dict) -> dict:
    """Run a tool from TOOL_DISPATCH. Catch everything to keep the loop alive."""
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}",
                "available": list(TOOL_DISPATCH)}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": "bad arguments", "details": str(e), "got": args}
    except Exception as e:
        return {"error": "tool raised", "details": str(e)}


def ask(
    question: str,
    model: str = DEFAULT_MODEL,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
    max_iterations: int = DEFAULT_MAX_ITER,
    temperature: float = DEFAULT_TEMPERATURE,
    keep_alive: str = DEFAULT_KEEP_ALIVE,
    verbose: bool = False,
) -> dict:
    """Run the agentic loop until the model returns a content answer.

    Returns {"answer", "tool_calls", "iterations", "model", "elapsed_sec"}.
    """
    t_start = time.perf_counter()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": question},
    ]
    tool_trace: list[dict] = []

    for iteration in range(1, max_iterations + 1):
        _log(f"iteration {iteration}: calling {model}", verbose)
        try:
            data = _call_chat(messages, model, ollama_host, temperature, keep_alive)
        except requests.exceptions.RequestException as e:
            return {
                "answer": f"[ERROR] Ollama request failed: {e}",
                "tool_calls": tool_trace, "iterations": iteration,
                "model": model, "elapsed_sec": round(time.perf_counter() - t_start, 2),
            }

        msg = data.get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        # append the assistant message to history (Ollama expects the round-trip)
        messages.append({
            "role": "assistant",
            "content": content,
            **({"tool_calls": tool_calls} if tool_calls else {}),
        })

        if not tool_calls:
            elapsed = round(time.perf_counter() - t_start, 2)
            _log(f"final answer in {elapsed}s after {iteration} iter(s)", verbose)
            return {
                "answer":      content,
                "tool_calls":  tool_trace,
                "iterations":  iteration,
                "model":       model,
                "elapsed_sec": elapsed,
            }

        for tc in tool_calls:
            fn_call = tc.get("function") or {}
            name    = fn_call.get("name", "")
            raw_args = fn_call.get("arguments", {})
            # Ollama / Qwen3 returns args as dict; some builds give a JSON string. Handle both.
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            else:
                args = raw_args or {}
            _log(f"  tool_call → {name}({args})", verbose)

            result = _execute_tool(name, args)
            tool_trace.append({
                "name":           name,
                "args":           args,
                "result_summary": _truncate_for_summary(result, 240),
            })
            messages.append({
                "role":    "tool",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    # Hit max_iterations without a final content answer
    elapsed = round(time.perf_counter() - t_start, 2)
    return {
        "answer": (content or "[max_iterations exhausted]"),
        "tool_calls":  tool_trace,
        "iterations":  max_iterations,
        "model":       model,
        "elapsed_sec": elapsed,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--model",       default=DEFAULT_MODEL)
    ap.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST)
    ap.add_argument("--max-iter",    type=int,   default=DEFAULT_MAX_ITER)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--verbose",     action="store_true")
    ap.add_argument("--json",        action="store_true")
    args = ap.parse_args()

    res = ask(args.question, model=args.model, ollama_host=args.ollama_host,
              max_iterations=args.max_iter, temperature=args.temperature,
              verbose=True)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
        return

    print(res["answer"])
    if res["tool_calls"]:
        print(f"\n--- Tool trace ({len(res['tool_calls'])} call(s), {res['iterations']} iteration(s), "
              f"{res['elapsed_sec']}s) ---", file=sys.stderr)
        for tc in res["tool_calls"]:
            print(f"  · {tc['name']}({json.dumps(tc['args'], ensure_ascii=False)})", file=sys.stderr)
            print(f"      → {tc['result_summary']}", file=sys.stderr)


if __name__ == "__main__":
    main()
