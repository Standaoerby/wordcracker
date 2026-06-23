#!/usr/bin/env python3
"""prewarm_author_propn.py — warm the author-NER union cache (WP-A, 2.7.37).

`affinity_by_author` builds an author-level proper-noun set (union of
`_book_propn_set` over the author's books) as the structural shield that drops
leaked toponyms/character names like `dunwich` from keyness output. The first
request for a prolific author pays the cold NER cost (~5-15s per book over the
author's books). Run this once on Server-on-Wheels (where the raw text lives)
so prod never serves a multi-minute cold `affinity_by_author` / `compare_authors`
for the commonly-compared authors.

Per-book NER is cached shared (`book_propn_cache/<id>.json`), so this is often
mostly warm already from affinity_by_book / book_archaic; the per-slug union is
written to `author_propn_cache/<slug>.v1.json`.

Usage (dev-overlay on SOW, where /workspace/raw_text exists):
    python -m scripts.prewarm_author_propn
    python -m scripts.prewarm_author_propn '^Poe,' '^Lovecraft, H'
"""
from __future__ import annotations

import sys
import time

try:
    from scripts.rag_tools import _author_propn_set, _slug
except ImportError:  # bare-name: scripts/ on sys.path
    from rag_tools import _author_propn_set, _slug

# Commonly-compared / prolific authors. The eyeball set from the spec
# (Poe, Lovecraft, Doyle, Wodehouse) plus a few habitually compared classics.
# Regexes mirror the `^Surname,` form used across the tools.
DEFAULT_AUTHORS = [
    "^Poe, Edgar Allan",
    "^Lovecraft, H. P.",
    "^Doyle, Arthur Conan",
    "^Wodehouse, P. G.",
    "^Dickens, Charles",
    "^Austen, Jane",
    "^Twain, Mark",
    "^Wilde, Oscar",
]


def main() -> int:
    authors = sys.argv[1:] or DEFAULT_AUTHORS
    rc = 0
    for regex in authors:
        slug = _slug(regex)
        t0 = time.perf_counter()
        try:
            propn = _author_propn_set(regex, slug)
            print(f"[ok] {regex!r} (slug={slug}): {len(propn)} propn tokens "
                  f"in {time.perf_counter()-t0:.1f}s")
        except Exception as e:  # noqa: BLE001 — batch tool, keep going
            rc = 1
            print(f"[err] {regex!r} (slug={slug}): {e}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
