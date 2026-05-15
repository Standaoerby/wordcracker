#!/usr/bin/env python3
"""
Tag affinity CSV rows with NER labels derived from raw text.

Runs spaCy en_core_web_trf (transformer NER on GPU) over a folder of raw
UTF-8 text files, collects every entity surface form into a set keyed by
its lowercased text, then enriches the affinity CSV with two columns:

  ner_label       -- the most-frequent label seen for this surface form
                     (PERSON, GPE, ORG, LOC, NORP, FAC, EVENT, WORK_OF_ART,
                      LAW, LANGUAGE, DATE, TIME, MONEY, QUANTITY, ORDINAL,
                      CARDINAL, PRODUCT). Empty if the form was never seen
                     as an entity.
  is_proper_noun  -- "1" if ner_label is one of {PERSON, GPE, LOC, ORG,
                     NORP, FAC} else "0".

Also writes a *_clean.csv with proper-noun rows dropped, and a JSON
summary.

Usage:
  python ner_filter_affinity.py \
    --raw-dir   /workspace/wodehouse_raw \
    --affinity  /workspace/spgc/derived/wodehouse_affinity.csv \
    --out-dir   /workspace/spgc/derived \
    --slug      wodehouse \
    --batch-size 4
"""
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from tqdm import tqdm

GUTENBERG_HEADER = re.compile(r"\*\*\* START OF (?:THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.IGNORECASE | re.DOTALL)
GUTENBERG_FOOTER = re.compile(r"\*\*\* END OF (?:THE|THIS) PROJECT GUTENBERG.*", re.IGNORECASE | re.DOTALL)
PROPER_LABELS = {"PERSON", "GPE", "LOC", "ORG", "NORP", "FAC"}


def strip_gutenberg(text: str) -> str:
    """Trim Gutenberg legal header and footer if present."""
    m = GUTENBERG_HEADER.search(text)
    if m:
        text = text[m.end():]
    m = GUTENBERG_FOOTER.search(text)
    if m:
        text = text[:m.start()]
    return text


def load_raw_texts(raw_dir: Path):
    files = sorted(raw_dir.glob("*.txt"))
    for f in files:
        with open(f, encoding="utf-8", errors="replace") as fh:
            yield f.name, strip_gutenberg(fh.read())


def chunk_text(text: str, chunk_chars: int = 100_000):
    for i in range(0, len(text), chunk_chars):
        yield text[i:i + chunk_chars]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", required=True, type=Path)
    ap.add_argument("--affinity", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--slug", default="author")
    ap.add_argument("--model", default="en_core_web_sm",
                    help="spaCy model. en_core_web_sm is fast on CPU (good for proper nouns); "
                         "en_core_web_trf is more accurate but needs working spaCy GPU stack.")
    ap.add_argument("--gpu", action="store_true", help="try to enable GPU (only useful for trf model)")
    ap.add_argument("--batch-size", type=int, default=8, help="spaCy nlp.pipe batch size")
    ap.add_argument("--chunk-chars", type=int, default=100_000)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[load] spaCy model: {args.model}")
    import spacy
    if args.gpu:
        try:
            from thinc.api import set_gpu_allocator, require_gpu
            set_gpu_allocator("pytorch")
            require_gpu()
            print("  GPU enabled")
        except Exception as e:
            print(f"  GPU setup failed, falling back to CPU: {e}")
    nlp = spacy.load(args.model)
    print(f"  loaded: {nlp.meta['name']} v{nlp.meta['version']}")

    # surface (lowercased) -> Counter({label: count})
    surface_labels: dict[str, Counter] = defaultdict(Counter)
    entity_total = 0

    raw_files = list(args.raw_dir.glob("*.txt"))
    print(f"[ner] scanning {len(raw_files)} raw text file(s) in {args.raw_dir}")

    for fname, text in tqdm(list(load_raw_texts(args.raw_dir)), desc="files", unit="file"):
        for chunk in chunk_text(text, args.chunk_chars):
            doc = nlp(chunk)
            for ent in doc.ents:
                surface_labels[ent.text.lower()][ent.label_] += 1
                entity_total += 1

    print(f"  entities found: {entity_total:,}")
    print(f"  unique surface forms: {len(surface_labels):,}")

    # majority label per surface form
    surface_top: dict[str, str] = {
        s: lbls.most_common(1)[0][0] for s, lbls in surface_labels.items()
    }

    print(f"[affinity] {args.affinity}")
    with open(args.affinity, encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        rows = list(rd)
    print(f"  rows: {len(rows)}")

    out_full = args.out_dir / f"{args.slug}_affinity_ner.csv"
    out_clean = args.out_dir / f"{args.slug}_affinity_clean.csv"

    proper_count = 0
    fieldnames = list(rows[0].keys()) + ["ner_label", "is_proper_noun"]
    with open(out_full, "w", encoding="utf-8", newline="") as fh_full, \
         open(out_clean, "w", encoding="utf-8", newline="") as fh_clean:
        w_full = csv.DictWriter(fh_full, fieldnames=fieldnames)
        w_clean = csv.DictWriter(fh_clean, fieldnames=fieldnames)
        w_full.writeheader()
        w_clean.writeheader()
        for r in rows:
            word = r["word"].lower()
            label = surface_top.get(word, "")
            is_pn = "1" if label in PROPER_LABELS else "0"
            r["ner_label"] = label
            r["is_proper_noun"] = is_pn
            w_full.writerow(r)
            if is_pn == "0":
                w_clean.writerow(r)
            else:
                proper_count += 1

    print(f"[write] {out_full}  ({len(rows)} rows)")
    print(f"[write] {out_clean}  ({len(rows) - proper_count} non-proper rows, dropped {proper_count} proper nouns)")

    summary = {
        "model": args.model,
        "raw_files": len(raw_files),
        "entities_found": entity_total,
        "unique_surface_forms": len(surface_labels),
        "affinity_rows": len(rows),
        "proper_noun_rows_dropped": proper_count,
        "clean_rows": len(rows) - proper_count,
        "label_distribution": dict(Counter(surface_top.values()).most_common()),
    }
    out_json = args.out_dir / f"{args.slug}_ner_filter_meta.json"
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[write] {out_json}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
