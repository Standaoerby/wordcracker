"""Final-audit suite: hit every registered tool with representative args
and check it doesn't crash. This is what we run at the end of v2 before
tagging stable. NOT a unit test — talks to the live container.

Run on the host:
    docker compose exec -T gutenberg-lab \
      python -u /workspace/tests/v2/test_all_tools.py

Outputs a Markdown report with per-tool verdict (pass/fail), runtime,
result size, and any warnings. Exit 0 only when every tool passes.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/workspace")
sys.path.insert(0, "/workspace/scripts")


# Sane defaults for each tool — picked to actually exercise the code path
# without burning chat-budget seconds. Use authors/books that we KNOW are
# in SPGC (verified via tests/v2/check_titles.py).
PROBES = [
    # corpus_meta
    ("corpus_overview",          {}),
    # books
    ("find_book",                {"title": "Pride and Prejudice", "top": 3}),
    ("affinity_by_book",         {"pg_id": "PG1342", "top": 10}),
    ("top_ngrams_by_book",       {"pg_id": "PG345", "n": 1, "top": 10}),
    ("book_readability",         {"pg_id": "PG1342"}),
    ("book_archaic_words",       {"pg_id": "PG345", "top": 10}),
    # authors
    ("author_metadata",          {"author_regex": "^Doyle,"}),
    ("top_authors_by",           {"metric": "books", "top": 5}),
    ("top_authors_by_country",   {"country": "GB", "top": 5}),
    ("affinity_by_author",       {"author_regex": "^Wodehouse,", "top": 10,
                                  "min_corpus_count": 100}),
    ("compare_authors",          {"author1_regex": "^Doyle,",
                                  "author2_regex": "^Wodehouse,", "top": 10}),
    ("author_profile",           {"author_regex": "^Doyle,"}),
    ("author_influences",        {"author_regex": "^Doyle,", "top": 5}),
    # words
    ("word_contexts",            {"author_regex": "^Wodehouse,",
                                  "word": "wicket", "max_samples": 3}),
    ("word_contexts_global",     {"word": "ajar", "k": 3}),
    ("word_collocates",          {"scope": {"author": "^Wodehouse,"},
                                  "word": "wicket", "top": 5}),
    ("word_freq_timeline",       {"word": "radio", "bucket_years": 25}),
    ("words_disappearing_after", {"year": 1920, "top": 5}),
    ("emotion_collocates",       {"scope": {"author": "^Poe,"},
                                  "emotion": "fear", "top": 5}),
    ("word_pos_distribution",    {"scope": {"book": "PG1342"},
                                  "word": "light"}),
    ("word_etymology",           {"word": "blue"}),
    ("find_words_by_etymology",  {"scope": {"author": "^Tolkien,"},
                                  "family": "germanic", "top": 5}),
    ("lemma_profile",            {"lemma": "civility"}),
    # search
    ("lexical_search",           {"query": "ajar", "k": 3}),
    ("hybrid_search",            {"query": "ajar", "k": 5, "per_retriever": 10}),
    # learning
    ("learning_words",           {"scope": {"book": "PG1342"},
                                  "level": "intermediate", "top": 5}),
    # NB: enrich_word and export_word_list and bulk_enrich are intentionally
    # left out — they require LLM round-trip / write to disk; covered by
    # the standalone learning-flow test.
]


def run() -> int:
    from scripts.v2.tool_registry import dispatch
    # Force v2 tools to load so they shadow the v1 path where migrated.
    from scripts.v2 import tools  # noqa: F401

    rows = []
    fails = 0
    for tool_name, args in PROBES:
        t0 = time.perf_counter()
        try:
            r = dispatch(tool_name, args)
        except Exception as e:
            elapsed = time.perf_counter() - t0
            rows.append((tool_name, "CRASH", elapsed, str(e)[:120], 0, []))
            fails += 1
            print(f"  CRASH {tool_name}: {e}")
            continue
        elapsed = time.perf_counter() - t0
        size = len(json.dumps(r.data, default=str)) if r.data is not None else 0
        warnings = [w.code for w in (r.warnings or [])]
        verdict = "PASS" if r.ok else "FAIL"
        if not r.ok:
            fails += 1
        rows.append((tool_name, verdict, elapsed, "", size, warnings))
        print(f"  {verdict:5s} {tool_name:30s} {elapsed:5.2f}s data={size}b warn={warnings}")

    # Emit markdown report
    out = ["# All-tools audit", ""]
    out.append("| Tool | Verdict | Time | Data bytes | Warnings |")
    out.append("|---|---|---:|---:|---|")
    for tool_name, verdict, elapsed, err, size, warnings in rows:
        wstr = ", ".join(warnings) or "—"
        out.append(f"| {tool_name} | {verdict} | {elapsed:.2f}s | {size} | {wstr} |")
        if err:
            out.append(f"  - error: `{err}`")
    out.append("")
    pass_n = sum(1 for r in rows if r[1] == "PASS")
    out.append(f"**Summary:** {pass_n}/{len(rows)} pass, {fails} fail/crash")
    Path("/workspace/test_all_tools_audit.md").write_text(
        "\n".join(out), encoding="utf-8")
    print(f"\n{pass_n}/{len(rows)} pass — report: /workspace/test_all_tools_audit.md")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
