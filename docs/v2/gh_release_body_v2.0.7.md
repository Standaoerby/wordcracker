# wordcracker v2.0.7 — bugfix release

Patch release fixing 3 issues caught during v2.0 stabilization. No
breaking changes; functional retest 40/40 stable.

## Fixed

**Q5 routing → book_compare** ([commit 0a2062a](https://github.com/Standaoerby/wordcracker/commit/0a2062a))
Two-author + two-book queries («слова у Диккенса в "Bleak House", но не у
Твена в "Adventures of Huckleberry Finn"») were classified as
author_compare and surfaced character names (Pickwick / Weller / Heep /
Nickleby / Squeers). Added a high-priority intent rule for the «X in
"BookA" but Y in "BookB"» phrasing — routes to book_compare which runs
affinity_by_book with `exclude_proper_nouns=True` on the primary book.

**compare_authors PROPN floor → 2000** ([commit 0a2062a](https://github.com/Standaoerby/wordcracker/commit/0a2062a))
`min_corpus_count=500` leaked character names that occur ~1000-1800
times via commentaries/adaptations. Bumped to 2000 in
`_plan_author_compare`. Actual stylistic markers
(cheerily/drawing-room/villainous) sit far above that threshold.

**Etymology multi-family extraction** ([commit 0a2062a](https://github.com/Standaoerby/wordcracker/commit/0a2062a))
Intro text suggested «c этимологией latin/french» as a complex example.
Copy-pasted by a user → `_find_etymology` returned both families as
one string → `find_words_by_etymology` rejected unknown family. Now
picks the *first-appearing* canonical family by position (latin wins in
«latin/french»). Intro example also simplified to a single-intent query
that always works.

## Verification

- Functional 40/40 = 100% pass-like (31 pass / 3 clarify / 6 OOS)
- All-tools audit 25/25 pass
- Unit tests 184/184
- Critic: 9 clean / 23 flagged on 40-question bench (improved from 4/28
  in v2.0.6 via critic over-flag guard)
- Median wall-clock ~14s, p95 ~21s

## Updated

- 7 «Отловленные баги» documented & closed in
  [wordcracker/Отловленные баги.md](https://github.com/Standaoerby/wordcracker/blob/main/docs/v2/RELEASE_NOTES.md)
- README.md added to repo root
- RELEASE_NOTES.md, test_bench, test_report all in `docs/v2/`
