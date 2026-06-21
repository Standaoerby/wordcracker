#!/usr/bin/env python3
"""One-shot, bounded cache hygiene for `word_dictionary.json` (2.7.35, WP-C-ii).

Two passes, both idempotent:

  Pass 1 — pure data, no LLM, always runs:
    any entry with `archaic == True AND proper_noun == True` → `archaic = False`.
    A name is never an archaic *word* (RAG_TASK §1.2: galatz-class). This makes
    the cache self-consistent with the runtime NER gate (book_archaic_words
    WP-B) and the sharpened ENRICH_PROMPT.

  Pass 2 — LLM re-enrich of a BOUNDED violator list, only with --enrich and a
    reachable Ollama:
      * the dated-referent offenders (calèche, chloral, galatz, …) and
      * the WP-A forms removed from the seed (amongst/amidst/ought/…), in case
        an old enrich run cached them archaic=True so they'd resurface via the
        cache path.
    Each is re-run through enrich_word(force_refresh=True) under the new prompt.
    We do NOT re-enrich the whole cache (VRAM / time — RAG_TASK §8).

Dry-run by default: prints what WOULD change. Pass --apply to persist pass 1
(and --enrich to additionally run pass 2). Safe to run repeatedly.

Run on SOW (the box with word_dictionary.json + a warm Ollama):
    python scripts/migrate_archaic_cache.py            # dry-run, report only
    python scripts/migrate_archaic_cache.py --apply    # pass 1 (data) only
    python scripts/migrate_archaic_cache.py --apply --enrich   # + pass 2 (LLM)
"""
from __future__ import annotations

import argparse
import os
import sys

# Reuse the canonical loader/saver/enricher so paths + cache shape stay in one
# place. scripts/ may or may not be on sys.path depending on how this is run.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import learning_tools as lt  # noqa: E402

# Known dated-referent / mis-tagged offenders called out in the RAG_TASK, plus
# the WP-A forms pruned from the seed (so a stale archaic=True cache entry can't
# re-introduce them through the enrich-cache path of book_archaic_words).
_VIOLATORS = [
    # dated referents — the object is old, the word is not
    "calèche", "caleche", "chloral", "corset", "brougham", "telegram",
    # mis-tagged place/person names that leaked in as archaic
    "galatz", "varna", "bistritz", "borgo", "whitby",
    # WP-A seed removals (standard-but-formal modern English / live homographs)
    "amongst", "amidst", "ought", "albeit", "hence", "ado", "bespoke",
    "fortnight", "clad", "smitten", "wrought", "aye",
]


def pass1_fix_propn_archaic(cache: dict) -> list[str]:
    """archaic=True AND proper_noun=True → archaic=False. Returns changed keys."""
    changed = []
    for key, info in cache.items():
        if not isinstance(info, dict):
            continue
        if info.get("archaic") is True and info.get("proper_noun") is True:
            info["archaic"] = False
            changed.append(key)
    return changed


def _ollama_reachable() -> bool:
    try:
        import requests
        host = os.environ.get("OLLAMA_HOST", lt.OLLAMA_HOST)
        requests.get(f"{host}/api/version", timeout=3).raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  Ollama not reachable ({e}); skipping pass 2", file=sys.stderr)
        return False


def pass2_reenrich_violators(cache: dict, apply: bool) -> list[str]:
    """Force-refresh the bounded violator list under the new ENRICH_PROMPT.

    Operates per (word, lang) key already present in the cache — we never
    invent new entries, only correct existing ones. Returns re-enriched keys.
    """
    touched = []
    present = {}  # word -> set of langs seen in cache
    for key in cache:
        word, _, lang = key.partition("|")
        present.setdefault(word, set()).add(lang or "ru")
    for word in _VIOLATORS:
        for lang in present.get(word.lower(), ()):  # type: ignore[arg-type]
            key = f"{word.lower()}|{lang}"
            before = dict(cache.get(key, {}))
            if not apply:
                print(f"  [dry] would re-enrich {key} "
                      f"(was archaic={before.get('archaic')}, "
                      f"propn={before.get('proper_noun')})")
                touched.append(key)
                continue
            res = lt.enrich_word(word, target_lang=lang, force_refresh=True)
            if res.get("error"):
                print(f"  ! enrich failed for {key}: {res['error']}",
                      file=sys.stderr)
                continue
            print(f"  re-enriched {key}: archaic "
                  f"{before.get('archaic')} → {res.get('archaic')}, "
                  f"propn {before.get('proper_noun')} → {res.get('proper_noun')}")
            touched.append(key)
    return touched


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="persist changes (default: dry-run)")
    ap.add_argument("--enrich", action="store_true",
                    help="also run pass 2 (LLM re-enrich of violators)")
    args = ap.parse_args()

    cache = lt._load_word_dict()
    if not cache:
        print(f"word_dictionary.json empty or missing at {lt.WORD_DICT_PATH}")
        return 0
    print(f"loaded {len(cache)} cache entries from {lt.WORD_DICT_PATH}")

    p1 = pass1_fix_propn_archaic(cache)
    print(f"pass 1: {len(p1)} entries archaic=True∧proper_noun=True → "
          f"archaic=False" + (" [dry-run]" if not args.apply else ""))
    for k in p1:
        print(f"  - {k}")

    p2 = []
    if args.enrich and (not args.apply or _ollama_reachable()):
        print("pass 2: re-enriching bounded violator list…")
        p2 = pass2_reenrich_violators(cache, apply=args.apply)

    if args.apply:
        lt._save_word_dict(cache)
        print(f"applied: saved {len(cache)} entries "
              f"({len(p1)} propn-fixed, {len(p2)} re-enriched)")
    else:
        print("dry-run: nothing written. Re-run with --apply (and optionally "
              "--enrich) to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
