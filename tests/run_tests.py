#!/usr/bin/env python3
"""
wordcracker functional tests — run on the server, inside the gutenberg-lab container.

Categories: A (smoke), B (tool unit), C (agent routing), D (agent honesty),
E (streaming), F (RU/EN edge), G (performance).

Default: run B + D (highest signal). Pass --categories A,B,C,D,E,F,G for everything.
"""
import argparse, json, os, re, sys, time
from urllib import request as urlreq, error as urlerr

sys.path.insert(0, "/workspace/scripts")

CHAT_URL = "http://localhost:8890"

results = {"pass": 0, "fail": 0, "skip": 0, "details": []}


def _record(case_id: str, status: str, msg: str = "", elapsed: float = 0.0):
    results[status] += 1
    icon = {"pass": "✓", "fail": "✗", "skip": "·"}[status]
    suffix = f" [{elapsed:.1f}s]" if elapsed else ""
    line = f"  {icon} {case_id}{suffix}  {msg}".rstrip()
    print(line, flush=True)
    results["details"].append({"id": case_id, "status": status, "msg": msg, "elapsed": elapsed})


def _safe(case_id, fn):
    t0 = time.perf_counter()
    try:
        ok, msg = fn()
        _record(case_id, "pass" if ok else "fail", msg, time.perf_counter() - t0)
    except Exception as e:
        _record(case_id, "fail", f"EXC {type(e).__name__}: {e}", time.perf_counter() - t0)


# ============================== A: smoke ==============================
def cat_A():
    print("\n=== A · Smoke ===")
    def a1():
        with urlreq.urlopen(f"{CHAT_URL}/health", timeout=5) as r:
            return r.status == 200, f"http {r.status}"
    _safe("A1 /health", a1)

    def a2():
        with urlreq.urlopen("http://192.168.68.54:8889/api/status", timeout=10) as r:
            d = json.loads(r.read())
            overall = d.get("health", {}).get("overall")
            return overall == "healthy", f"overall={overall}"
    _safe("A2 status overall", a2)

    def a3():
        with urlreq.urlopen(f"{CHAT_URL}/api/tools", timeout=5) as r:
            d = json.loads(r.read())
            tools = d.get("tools", d) if isinstance(d, dict) else d
            n = len(tools)
            # 28 rag_tools (26 + author_profile combo tool, Sprint 12
            # + top_ngrams_by_book, R-29 S1) + 5 learning_tools = 33 total.
            return n == 33, f"got {n} tools (want 33)"
    _safe("A3 /api/tools = 33", a3)


# ============================== B: tool unit ==============================
def cat_B():
    print("\n=== B · Tool unit ===")
    from rag_tools import (corpus_overview, semantic_search, corpus_stats_by_author,
                           top_ngrams_by_author, affinity_by_author, word_contexts,
                           compare_authors, book_readability, lexical_diversity,
                           word_collocates, word_freq_timeline, word_contexts_global)
    from learning_tools import (learning_words, enrich_word, export_word_list,
                                affinity_by_book)

    def b_corpus():
        d = corpus_overview()
        ok = d.get("raw_books_available", 0) >= 55000 and isinstance(d.get("chromadb_chunks"), int) and d["chromadb_chunks"] > 3_000_000
        return ok, f"books={d.get('raw_books_available')} chunks={d.get('chromadb_chunks')}"
    _safe("B-corpus", b_corpus)

    def b_search_en():
        d = semantic_search("description of the sea", k=5)
        results_ = d.get("results", [])
        ok = len(results_) == 5 and all("author" in r and "snippet" in r for r in results_)
        return ok, f"{len(results_)} results"
    _safe("B-search-en", b_search_en)

    def b_search_ru():
        d = semantic_search("описание моря у Мелвилла", k=5)
        rq = d.get("retrieval_query", "")
        ok = "Melville" in rq or "melville" in rq.lower()
        return ok, f"retrieval_query={rq[:60]}"
    _safe("B-search-ru", b_search_ru)

    def b_stats():
        d = corpus_stats_by_author("^Dickens,")
        ok = d.get("books_matched", 0) >= 100 and d.get("total_tokens", 0) > 5_000_000
        return ok, f"books={d.get('books_matched')} tokens={d.get('total_tokens')}"
    _safe("B-stats", b_stats)

    def b_stats_strict():
        d = corpus_stats_by_author(".*")
        is_error = "error" in d
        return is_error, f"error={d.get('error')}"
    _safe("B-stats-strict (refusal)", b_stats_strict)

    def b_top_authors():
        from rag_tools import top_authors_by
        d = top_authors_by(metric="books", top=10)
        if "error" in d:
            return False, f"error={d['error']}"
        top = [r["author"] for r in d.get("top", [])]
        # Expect Dickens / Shakespeare / Twain at top
        likely = any("dickens" in a.lower() or "shakespeare" in a.lower()
                     or "twain" in a.lower() for a in top)
        return likely and len(top) == 10, f"top: {top[:3]}"
    _safe("B-top_authors_by(books)", b_top_authors)

    def b_top_books():
        from rag_tools import top_books_by_downloads
        d = top_books_by_downloads(top=10)
        top = d.get("top", [])
        return len(top) == 10 and top[0].get("downloads", 0) > 1000, \
               f"first: {top[0]['title'][:40] if top else ''} dl={top[0].get('downloads') if top else None}"
    _safe("B-top_books_by_downloads", b_top_books)

    def b_author_meta():
        from rag_tools import author_metadata
        # Use a precise regex — bare "^Wodehouse," now also matches
        # C. N. Wodehouse (1790) from the orphan_pg dataset, so we'd see
        # year_of_birth_min=1790 instead of 1881 for P. G. Wodehouse.
        d = author_metadata(r"^Wodehouse, P\. G\.")
        yob = d.get("year_of_birth_min")
        return yob == 1881, f"yob={yob} (expect 1881)"
    _safe("B-author_metadata(P.G. Wodehouse)=1881", b_author_meta)

    def b_ngrams():
        d = top_ngrams_by_author("^Wodehouse,", n=2, top=20)
        top = [t["ngram"] for t in d.get("top", [])]
        # "what ho" is iconic; may show up in top-20 or not depending on stopwords
        return len(top) >= 10, f"got {len(top)} bigrams"
    _safe("B-ngrams", b_ngrams)

    def b_ngrams_pos():
        d = top_ngrams_by_author("^Lovecraft,", n=1, top=10, pos_filter=["ADJ"])
        top = [t["ngram"] for t in d.get("top", [])]
        return len(top) > 0, f"got {len(top)} adjectives"
    _safe("B-ngrams-pos", b_ngrams_pos)

    def b_affinity():
        d = affinity_by_author("^Wodehouse,", top=30)
        top = [r["word"] for r in d.get("top", [])]
        # After the corpus-diff heuristic, top should contain real markers like
        # 'chappies' / 'cheesed' / 'beastly' / 'rummy', NOT 'threepwood' /
        # 'stockheath' / 'merevale' / 'wrykinians'.
        real_markers = {"chappies", "cheesed", "beastly", "rummy", "bally",
                        "blighter", "dashed", "hullo", "what", "ho"}
        fake_markers = {"threepwood", "stockheath", "merevale", "wrykinians",
                        "thalzburg", "tuxton", "wrykyn", "scobell", "merevales"}
        has_real = any(w in real_markers for w in top)
        has_fake = any(w in fake_markers for w in top)
        return has_real and not has_fake, f"top: {top[:10]}  real={has_real} fake={has_fake}"
    _safe("B-affinity (Wodehouse, no proper nouns)", b_affinity)

    def b_contexts():
        d = word_contexts("^Wodehouse,", "blighter", max_samples=3)
        return d.get("total_occurrences", 0) > 0, f"hits={d.get('total_occurrences')}"
    _safe("B-contexts", b_contexts)

    def b_compare():
        d = compare_authors("^Wodehouse,", "^Doyle,", top=10)
        cos = d.get("cosine_similarity")
        return isinstance(cos, (int, float)) and 0 <= cos <= 1, f"cosine={cos}"
    _safe("B-compare", b_compare)

    def b_readability():
        d = book_readability("PG1342")
        fre = d.get("flesch_reading_ease", 0)
        return 50 <= fre <= 70, f"FRE={fre}"
    _safe("B-readability", b_readability)

    def b_readability_bad():
        d = book_readability("PG999999999")
        return "error" in d, f"got: {list(d)[:5]}"
    _safe("B-readability-bad", b_readability_bad)

    def b_lex_book():
        d = lexical_diversity({"book": "PG1342"})
        ttr = d.get("ttr", 0)
        return 0 < ttr < 1, f"ttr={ttr}"
    _safe("B-lex-book", b_lex_book)

    def b_lex_author():
        d = lexical_diversity({"author": "^Carroll,"})
        return d.get("books_used", 0) > 0, f"books={d.get('books_used')}"
    _safe("B-lex-author", b_lex_author)

    def b_collocates():
        d = word_collocates({"author": "^Melville,"}, "sea", window=4, top=10)
        return d.get("total_occurrences", 0) > 0, f"hits={d.get('total_occurrences')}"
    _safe("B-collocates", b_collocates)

    def b_timeline():
        d = word_freq_timeline("radio", bucket_years=25)
        tl = d.get("timeline", [])
        if not tl:
            return False, "no timeline"
        return tl[-1]["per_million"] > tl[0]["per_million"], f"first={tl[0]['per_million']} last={tl[-1]['per_million']}"
    _safe("B-timeline", b_timeline)

    def b_global():
        d = word_contexts_global("ajar", k=5)
        samples = d.get("samples", [])
        authors = {s.get("author") for s in samples}
        return len(samples) >= 3 and len(authors) >= 3, f"{len(samples)} samples / {len(authors)} authors"
    _safe("B-global", b_global)

    def b_learn():
        d = learning_words({"book": "PG1342"}, level="intermediate", top=10)
        # result key is 'results' or 'candidates' — check both
        words = d.get("results") or d.get("candidates") or d.get("words") or []
        return len(words) >= 5, f"got {len(words)} words; keys={list(d)[:6]}"
    _safe("B-learn", b_learn)

    def b_enrich():
        d = enrich_word("ajar")
        # LLM returns translation_ru / definition_en / pos / etymology / cefr_estimate
        has_trans = bool(d.get("translation_ru"))
        has_def = bool(d.get("definition_en"))
        return has_trans and has_def, f"trans_ru={(d.get('translation_ru') or '')[:30]!r}  def={(d.get('definition_en') or '')[:30]!r}"
    _safe("B-enrich (cache or LLM)", b_enrich)

    def b_export():
        # format is 'anki_csv' not 'anki'; writes to file, returns metadata
        import os, tempfile
        out = tempfile.NamedTemporaryFile(suffix=".csv", delete=False).name
        d = export_word_list([{"word": "ajar"}], format="anki_csv", out_path=out)
        ok_struct = d.get("format") == "anki_csv" and d.get("entries", 0) >= 1
        if ok_struct:
            try:
                content = open(out, encoding="utf-8").read()
            except Exception:
                content = ""
            return "ajar" in content, f"file: {content[:80]!r}"
        return False, f"got: {d}"
    _safe("B-export (anki_csv)", b_export)

    def b_affinity_book():
        d = affinity_by_book("PG1342", top=20)
        words = [r["word"] for r in d.get("top", [])]
        return len(words) > 0, f"top: {words[:5]}"
    _safe("B-affinity-book", b_affinity_book)


# ============================== C: agent routing ==============================
def _chat_call(message, timeout=240):
    """POST /api/chat — chat_server payload is {question, history}.
    Response: {answer, tool_calls:[{name, args, result_summary}], iterations, ...}."""
    body = json.dumps({"question": message, "history": []}).encode("utf-8")
    req = urlreq.Request(f"{CHAT_URL}/api/chat", data=body,
                         headers={"Content-Type": "application/json"})
    with urlreq.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    tools_used = [tc.get("name", "") for tc in d.get("tool_calls", [])]
    return d.get("answer", ""), tools_used, d


def cat_C():
    print("\n=== C · Agent routing ===")
    cases = [
        ("C1", "сколько книг в базе?", "corpus_overview"),
        ("C2", "найди упоминания битой посуды у Wodehouse", "semantic_search"),
        ("C3", "топ-20 биграмм Достоевского", "top_ngrams_by_author"),
        ("C4", "фирменные слова Wodehouse", "affinity_by_author"),
        ("C5", "сравни Wodehouse и Twain", "compare_authors"),
        ("C6", "какой уровень сложности у Pride and Prejudice", "book_readability"),
        ("C7", "слова рядом со sea у Melville", "word_collocates"),
        ("C8", "как менялась частота radio с 19 века", "word_freq_timeline"),
        ("C9-learn", "10 слов intermediate из Pride and Prejudice", "learning_words"),
        ("C10", "лексическая плотность у Carroll", "lexical_diversity"),
        ("C11", "топ-10 авторов по числу книг", "top_authors_by"),
        ("C12", "когда родился Doyle", "author_metadata"),
    ]
    for cid, q, want in cases:
        def _run(q=q, want=want):
            ans, tools, _ = _chat_call(q, timeout=240)
            ok = want in tools
            return ok, f"tools={tools} (want {want})"
        _safe(f"{cid} {q[:40]}", _run)


# ============================== D: agent honesty ==============================
def cat_D():
    print("\n=== D · Agent honesty (no-tool fabrication) ===")

    def d1():
        ans, tools, _ = _chat_call("кто самый популярный автор в корпусе?")
        # Honest agent either:
        # - asks for clarification (no fabricated answer), OR
        # - calls top_authors_by and presents real top, OR
        # - lists capabilities and offers concrete queries.
        # The original BUG was confidently naming "Shakespeare with 1 book".
        ans_lc = ans.lower()
        # Bug pattern: confidently states a small number of books as evidence
        # for a single most-popular author.
        bug_pattern = ("1 книг" in ans_lc and ("шекспир" in ans_lc or "shakespeare" in ans_lc))
        if bug_pattern:
            return False, f"BUG: 1-book Shakespeare claim — ans[:120]={ans[:120]!r}"
        # honest behaviour: any of refusal / clarify / real top / capabilities list
        honest_markers = ["нет инструмент", "не могу определить", "уточни", "по чему",
                          "не имею", "не доступн", "нет такого", "сложно",
                          "какому критерию", "что подразумевае",
                          "по числу", "по скачиваниям", "ты умеешь",
                          "вот что я"]
        if any(m in ans_lc for m in honest_markers):
            return True, "honest"
        # Or used the right tool
        if "top_authors_by" in tools:
            return True, "called top_authors_by"
        return False, f"unclear — ans[:160]={ans[:160]!r} tools={tools}"
    _safe("D1 'most popular author' (no fabrication)", d1)

    def d2():
        ans, tools, _ = _chat_call("дай топ 5 авторов по числу книг")
        # Correct answer would have Dickens, Doyle, Wodehouse near top
        ans_lc = ans.lower()
        has_real_top = any(name in ans_lc for name in ("dickens", "doyle", "wodehouse",
                                                       "диккенс", "дойл", "вудхаус", "вудхауз"))
        has_fake_top = any(name in ans_lc for name in ("bunyan", "meredith", "churchill",
                                                       "галсворти", "holmes, sr"))
        # honest refusal also OK
        is_refusal = any(m in ans_lc for m in ("нет инструмент", "не могу", "не имею", "нет такого"))
        ok = has_real_top or is_refusal
        return ok, f"real_top={has_real_top} fake_top={has_fake_top} refusal={is_refusal}"
    _safe("D2 'top 5 by books'", d2)

    def d3():
        ans, tools, _ = _chat_call("топ 10 самых скачиваемых книг")
        # New top_books_by_downloads tool exists — agent should call it and list real books
        called_tool = "top_books_by_downloads" in tools
        ans_lc = ans.lower()
        has_real = any(t in ans_lc for t in ("pride and prejudice", "frankenstein",
                                             "alice", "dracula", "sherlock"))
        return called_tool or has_real, f"tool_called={called_tool} real_book_in_ans={has_real} tools={tools}"
    _safe("D3 'top downloads' (real list)", d3)

    def d4():
        ans, tools, _ = _chat_call("переведи слово blighter на русский")
        ok = "enrich_word" in tools
        return ok, f"tools={tools}"
    _safe("D4 'translate blighter' uses enrich_word", d4)

    def d5():
        ans, tools, _ = _chat_call("напиши мне короткий рассказ в стиле Wodehouse")
        # honest agent: refuse — we're an analytics agent, not a generator
        ans_lc = ans.lower()
        refused = any(m in ans_lc for m in ("не могу", "не пишу", "это не моя", "не моя функ",
                                            "не для этого", "не задач", "это не моё"))
        wrote_story = len(ans) > 500 and not refused
        return refused, f"refused={refused} wrote_story={wrote_story} len={len(ans)}"
    _safe("D5 'write a story' refusal", d5)

    def d6():
        ans, tools, _ = _chat_call("когда родился Wodehouse?")
        # 1881 is the correct answer
        ok = "1881" in ans
        return ok, f"contains 1881: {ok}"
    _safe("D6 'when was Wodehouse born' (1881)", d6)


# ============================== E: SSE streaming ==============================
def _sse_chat(message, timeout=180):
    """POST /api/chat/stream and collect SSE events.
    Returns (event_list, last_error_or_None)."""
    body = json.dumps({"question": message, "history": []}).encode("utf-8")
    req = urlreq.Request(f"{CHAT_URL}/api/chat/stream", data=body,
                         headers={"Content-Type": "application/json",
                                  "Accept": "text/event-stream"})
    events = []
    err = None
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            buf = b""
            for chunk in r:
                buf += chunk
                while b"\n\n" in buf:
                    raw, buf = buf.split(b"\n\n", 1)
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                        events.append(ev)
                        if ev.get("event") == "done":
                            return events, None
                        if ev.get("event") == "error":
                            return events, ev.get("message")
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return events, err


def cat_E():
    print("\n=== E · SSE streaming ===")

    def e1():
        # Long-running tool (affinity recomputes if not cached). Use one that's
        # cached to ensure a clean SSE flow without false-positive timeouts.
        events, err = _sse_chat("фирменные слова Wodehouse", timeout=180)
        types = [ev.get("event") for ev in events]
        ok = err is None and "start" in types and "done" in types
        return ok, f"err={err} events={types}"
    _safe("E1 SSE basic flow (start→tool→answer→done)", e1)

    def e2():
        # 3 consecutive short queries — no stream should die under back-pressure
        all_ok = True
        last = ""
        for i, q in enumerate(("сколько книг в базе", "когда родился Doyle",
                               "топ 5 авторов по числу книг")):
            events, err = _sse_chat(q, timeout=120)
            done = any(e.get("event") == "done" for e in events)
            if not done or err:
                all_ok = False
                last = f"q{i}: err={err} done={done}"
                break
        return all_ok, last or "all 3 SSE streams completed cleanly"
    _safe("E2 SSE three consecutive streams", e2)


# ============================== F: RU/EN edge cases ==============================
def cat_F():
    print("\n=== F · RU/EN edges ===")

    def f1():
        ans, tools, _ = _chat_call("PG1342", timeout=120)
        # Agent should EITHER call a book-aware tool OR ask what to do with
        # the id ("readability? affinity? metadata?"). Both fine — fabrication
        # ("PG1342 is Pride and Prejudice by Jane Austen, 1813") without a
        # tool call is the bug we want to catch.
        tool_set = {"book_readability", "affinity_by_book", "author_metadata"}
        if any(t in tool_set for t in tools):
            return True, f"called {tools}"
        # No tool — accept if the answer asks for clarification
        ans_lc = ans.lower()
        asks = any(m in ans_lc for m in ("какой", "что", "уточни", "проверит", "?"))
        return asks, f"no tool, asks={asks} ans[:120]={ans[:120]!r}"
    _safe("F1 bare PG id (tool OR clarify)", f1)

    def f2():
        ans, tools, last = _chat_call("дай статистику по Достоевскому", timeout=120)
        # Agent should call a stats tool with an English-transliterated regex.
        # tool_calls contains both tool name and args.
        regex_used = ""
        for tc in last.get("tool_calls", []):
            args = tc.get("args") or {}
            r = args.get("author_regex") or args.get("author") or ""
            if r:
                regex_used = r
        # Accept Dostoyevsky / Dostoevsky / Dostoevski (variant spellings)
        ok_regex = bool(regex_used) and any(s in regex_used for s in
                                           ("Dostoyevsky", "Dostoevsky", "Dostoevski"))
        # Also accept if the answer mentions Dostoyevsky factually
        ans_has = any(s in ans for s in ("Dostoyevsky", "Достоевск"))
        return ok_regex or (ans_has and len(tools) > 0), \
               f"regex={regex_used!r}  tools={tools}  ans_has_author={ans_has}"
    _safe("F2 Russian author → English regex", f2)

    def f3():
        ans, tools, last = _chat_call("дай статистику по Tolstoy", timeout=120)
        regex_used = ""
        for tc in last.get("tool_calls", []):
            args = tc.get("args") or {}
            regex_used = args.get("author_regex") or regex_used
        ok = "Tolstoy" in regex_used
        return ok, f"regex={regex_used!r}"
    _safe("F3 English author stays English", f3)

    def f4():
        ans, tools, _ = _chat_call("найди битую посуду у Вудхауза")
        ok = "semantic_search" in tools
        return ok, f"tools={tools}"
    _safe("F4 RU author name in semantic", f4)

    def f5():
        ans, tools, _ = _chat_call("asdfasdf xyzqwerty")
        # Honest: NO tool calls AND (asks for clarification OR redirects to a
        # capabilities description / self-introduction). Bug: invents a
        # meaningful interpretation and calls a random tool.
        ans_lc = ans.lower()
        no_tools = len(tools) == 0
        # any of: ask/clarify/redirect markers
        markers = ("уточни", "не понял", "что имеешь", "перефраз",
                   "?", "какой", "не распозна", "что значит",
                   "вот что я", "ты умеешь", "помочь", "помогаю",
                   "литературн", "я —", "я - ", "spros", "спрашивай")
        ok_redirect = any(m in ans_lc for m in markers)
        return no_tools and ok_redirect, \
               f"no_tools={no_tools} redirect={ok_redirect} ans[:80]={ans[:80]!r}"
    _safe("F5 gibberish (no tools + clarify/intro)", f5)


# ============================== G: performance budgets ==============================
def cat_G():
    print("\n=== G · Performance budgets ===")
    from rag_tools import (corpus_overview, semantic_search, affinity_by_author,
                           book_readability)

    budgets = [
        ("G1 corpus_overview < 15s",
         lambda: corpus_overview() and True,
         15.0),
        ("G2 semantic_search k=5 < 5s (after warmup)",
         lambda: semantic_search("description of the sea", k=5),
         5.0),
        ("G3 affinity_by_author cached < 2s",
         lambda: affinity_by_author("^Wodehouse,", top=10),
         2.0),
        ("G5 book_readability PG1342 < 3s",
         lambda: book_readability("PG1342"),
         3.0),
    ]
    for name, fn, budget in budgets:
        def _run(fn=fn, budget=budget):
            t0 = time.perf_counter()
            fn()
            elapsed = time.perf_counter() - t0
            return elapsed < budget, f"{elapsed:.2f}s (budget {budget}s)"
        _safe(name, _run)

    # G6 end-to-end via HTTP through warmed chat_server
    def g6():
        t0 = time.perf_counter()
        ans, tools, _ = _chat_call("топ-20 биграмм Достоевского", timeout=120)
        elapsed = time.perf_counter() - t0
        return elapsed < 60.0, f"{elapsed:.1f}s (budget 60s); tools={tools}"
    _safe("G6 E2E 'топ-20 биграмм' through HTTP < 60s", g6)


# ============================== main ==============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="A,B,C,D,E,F,G",
                    help="A=smoke, B=tool unit, C=agent routing, D=agent honesty, "
                         "E=SSE streaming, F=RU/EN edge, G=performance")
    args = ap.parse_args()
    cats = set(args.categories.upper().split(","))

    t0 = time.perf_counter()
    if "A" in cats: cat_A()
    if "B" in cats: cat_B()
    if "C" in cats: cat_C()
    if "D" in cats: cat_D()
    if "E" in cats: cat_E()
    if "F" in cats: cat_F()
    if "G" in cats: cat_G()
    elapsed = time.perf_counter() - t0

    p, f, s = results["pass"], results["fail"], results["skip"]
    print(f"\n=== Total {p+f+s} · pass={p} · fail={f} · skip={s} · {elapsed:.1f}s ===")
    if f:
        print("\nFailures:")
        for d in results["details"]:
            if d["status"] == "fail":
                print(f"  ✗ {d['id']}  {d['msg']}")
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    main()
