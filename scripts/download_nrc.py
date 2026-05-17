#!/usr/bin/env python3
"""
download_nrc.py — fetch NRC Emotion Lexicon and save in compact JSON.

Downloads the wordlevel TSV from a public GitHub mirror (NRC-Emotion-
Lexicon-Wordlevel-v0.92.txt) and converts to:
    {word: ['anger', 'fear', 'negative', ...], ...}
keeping only emotions with association=1 — drops the 90% zeros.

Output: /data/spgc/derived/nrc_emotion_lexicon.json
Run once after install; the rag_tools _nrc_lexicon() helper lazy-reads
the JSON afterwards. ~14k words, ~200KB JSON.
"""
import json
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

URL = "https://raw.githubusercontent.com/dinbav/LeXmo/master/NRC-Emotion-Lexicon-Wordlevel-v0.92.txt"
OUT = Path("/workspace/spgc/derived/nrc_emotion_lexicon.json")


def main():
    print(f"[nrc] downloading {URL}", flush=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "wordcracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8")

    lex: dict[str, list[str]] = defaultdict(list)
    for line in raw.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        word, emotion, assoc = parts
        if assoc != "1":
            continue
        lex[word.lower()].append(emotion)

    lex_out = {w: sorted(es) for w, es in sorted(lex.items())}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(lex_out, fh, ensure_ascii=False)

    # Per-emotion counts for sanity
    counts = defaultdict(int)
    for es in lex_out.values():
        for e in es:
            counts[e] += 1
    print(f"[nrc] saved {len(lex_out):,} words to {OUT}", flush=True)
    print(f"[nrc] {sum(map(len, lex_out.values())):,} (word,emotion) pairs")
    for e, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {e:14s} {c:>5d} words")


if __name__ == "__main__":
    main()
