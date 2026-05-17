#!/usr/bin/env python3
"""
bulk_enrich_propn.py — pre-populate word_dictionary.json with proper_noun
verdicts for the top affinity words of popular authors. Closes the v1.1.5
mahaffy/arcady/algy class of residual PROPN that spaCy POS tagged as ADJ
on lowercase single-token input.

Strategy:
  1. Pick the top-N most-popular EN authors by downloads.
  2. For each: pull affinity_by_author(top=N_words) — these are the words
     most likely to confuse the propn filters.
  3. enrich_word every one. The LLM verdict goes to word_dictionary.json;
     subsequent affinity_by_author runs will automatically skip propn=True.

Result is purely additive — the cache only grows, no schema changes, no
prompt changes. Future fresh queries that touch any of these authors get
clean output for free.

Background batch, idempotent — already-enriched (word, lang) pairs skip.

CLI:
  python bulk_enrich_propn.py --top-authors 30 --top-words 30
"""
import argparse
import sys
import time

sys.path.insert(0, "/workspace/scripts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-authors", type=int, default=30,
                    help="how many popular authors to walk (default 30)")
    ap.add_argument("--top-words", type=int, default=30,
                    help="per-author affinity top to enrich (default 30)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap total enrich_word calls (0=no cap)")
    args = ap.parse_args()

    from rag_tools import top_authors_by, affinity_by_author, _log
    from learning_tools import enrich_word, _load_word_dict

    print(f"[bulk-enrich] picking top {args.top_authors} authors by downloads",
          flush=True)
    top = top_authors_by(metric="downloads", top=args.top_authors)
    rows = top.get("top", [])
    if not rows:
        print("  no authors returned")
        return

    cache = _load_word_dict()
    print(f"  cache currently has {len(cache):,} entries", flush=True)

    total_calls = 0
    total_skipped = 0
    total_new_propn = 0

    for ai, ar in enumerate(rows, 1):
        author = ar["author"]
        # Build a strict regex from the SPGC "Surname, ..." format
        surname = author.split(",", 1)[0]
        regex = f"^{surname},"
        print(f"\n[{ai}/{len(rows)}] {author}  regex={regex!r}", flush=True)
        try:
            aff = affinity_by_author(regex, top=args.top_words,
                                     min_corpus_count=0)
        except Exception as e:
            print(f"  affinity FAILED: {e}", flush=True)
            continue
        if "error" in aff:
            print(f"  affinity error: {aff['error']}", flush=True)
            continue
        words = [r["word"] for r in aff.get("top", [])]
        for w in words:
            key = f"{w.lower()}|ru"
            if key in cache:
                total_skipped += 1
                continue
            t0 = time.perf_counter()
            try:
                res = enrich_word(w, contexts=[""], lemma_hint=w, pos_hint="")
            except Exception as e:
                print(f"  enrich {w!r} ERROR: {e}", flush=True)
                continue
            total_calls += 1
            if res.get("proper_noun") is True:
                total_new_propn += 1
            verdict = "PROPN" if res.get("proper_noun") else f"{res.get('pos','?')}"
            print(f"  {w:24s}  {verdict:6s}  ({time.perf_counter()-t0:.1f}s)",
                  flush=True)
            if args.limit and total_calls >= args.limit:
                print(f"\n[bulk-enrich] hit --limit={args.limit}, stopping", flush=True)
                break
        if args.limit and total_calls >= args.limit:
            break

    print(f"\n[bulk-enrich] done: {total_calls} new enrichments, "
          f"{total_skipped} cache hits, {total_new_propn} new propn tags",
          flush=True)


if __name__ == "__main__":
    main()
