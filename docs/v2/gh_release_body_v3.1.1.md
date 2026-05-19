# wordcracker v3.1.1 — Two screenshots + a surname filter

Patch release on top of v3.1. Stan ran prod tests on 2026-05-19; three
distinct gaps surfaced. All three are fixed here. No new intents, no
breaking changes.

**Suite:** 637 unit tests / 0 failures (was 612 at v3.1; +16 typed
clarify + 16 polysemy/etymology + 9 surname filter — minus duplicates).

## Why these fixes

### 1. `polysemy для слова set` → tool error

The plan dispatched `word_pos_distribution({'scope': 'all_corpus',
'word': 'set'})`. v1 `word_pos_distribution` (rag_tools.py:1577)
rejects bare strings:

> `"bad scope; use {'book':PGid} | {'author':regex}"`

The user saw the v1 error message verbatim in chat. Critic was clean
because the tool returned a syntactically valid response, just with an
`error` field — nothing for the critic to flag against.

**Fix** (`scripts/v2/planner/plan.py:_plan_word_pos`): when no book /
author / period filter is present, widen the dispatched scope to
`{'author': '.*'}`. v1 `_select_books` resolves that to "all English
books"; `max_occurrences=200` caps walk-time to ~1s.

### 2. `germanic vs latinate ratio в Beowulf и Paradise Lost` → bare clarify

Three independent gaps stacked:

- **Beowulf** (PG16328) not in KNOWN_BOOKS
- **Paradise Lost** (PG26) not in KNOWN_BOOKS
- **John Milton** not in `AUTHOR_ALIASES_CURATED`
- **`german`** alias in COUNTRY_ALIASES substring-matched inside
  `germanic`, tagging the query as `country=DE` and pushing the
  planner toward the wrong recipe branch

Even after fixing all of the above, the planner had nowhere to route
a multi-book etymology-ratio question — no single tool computes "Latinate
share vs Germanic share across N books". Falls to clarify.

**Fix** (`scripts/v2/planner/entities.py` + `plan.py`):

1. Added Beowulf, Paradise Lost (en + ru declensions), Milton
   (en + ru) to the curated tables.
2. Rewrote `_find_country` to require word boundaries on Latin-script
   aliases. Cyrillic stems (русск, немецк) keep substring matching
   for natural declensional coverage. The `germanic` ≠ DE bug becomes
   impossible to reintroduce — there's a regression test.
3. New branch in `_smart_clarify_recipe` for the etymology-ratio
   pattern (`etymology_family + ≥2 books + ratio marker`). Surfaces a
   concrete per-book recipe:
   ```
   • find_words_by_etymology scope=book:PG16328 (Beowulf)
     family=germanic → high-affinity germanic words
   • find_words_by_etymology scope=book:PG16328 (Beowulf)
     family=latin    → high-affinity latin words
   • find_words_by_etymology scope=book:PG26 (Paradise Lost)
     family=germanic → high-affinity germanic words
   • find_words_by_etymology scope=book:PG26 (Paradise Lost)
     family=latin    → high-affinity latin words
   ```
   Plus the disclosure: this is affinity-based, not token-coverage.
   True coverage ratio = Sprint 20 backlog.

### 3. Conan Doyle «фирменные слова» → wall of character surnames

Stan's screenshot: top of Doyle's affinity list was almost entirely
character names — challenger / knolles / barrymore / holmes /
flannigan / stapleton / mcfarlane / baumgarten. All bypass the
existing three defences:

1. **Corpus-diff heuristic** — these surnames appear in *other* authors'
   books too (Oliver Wendell Holmes essays, Lionel Barrymore
   biographies, historical Knolles works), so `corpus_count -
   author_count >= max(10, author_count*0.5)` passes.
2. **spaCy PROPN drop on isolated lowercase tokens** — unreliable on
   ambiguous tokens, returns NOUN for "holmes" / "challenger" / "burger".
3. **word_dict.proper_noun flag** — only populated for words seen in
   prior `learning_words` runs.

The clean fix is a *positive* surname signal, not a negative POS one.

**Fix** (`scripts/v2/tools/authors/_surname_filter.py` — new module):

- **Curated literary character surnames** (~150 entries): full
  Sherlock universe (holmes/watson/lestrade/moriarty/barrymore/stapleton
  /mortimer/...), Challenger circle (challenger/summerlee/malone/
  roxton), Sir Nigel (knolles/alleyne/loring), plus Dickens / Austen /
  Bronte / Hardy / 19th-c Russians / Wodehouse / Lovecraft / Shakespeare
  / Twain / Melville / Hugo / Dumas.
- **PG-author surnames** lazy-loaded + mtime-cached from
  `/workspace/spgc/SPGC-metadata-2018-07-18.csv` (`author` column,
  `"Surname, Forename"` → `"surname"`). Missing file → empty set, no
  crash. On the server this is ~10k surnames.
- Applied in both `affinity_by_author` (v2 wrapper) and
  `affinity_by_book` (v2 wrapper) as a new defence layer after the
  existing self-name + literary-blacklist drops.

The filter is intentionally aggressive — if "smith" / "cooper" /
"baker" happen to be both real lexemes and surnames, they get dropped.
Signature-word output values cleanliness over completeness.

### Bonus — affinity formula audit

Stan also asked to verify the math. Source:
`scripts/spgc_author_affinity.py:104`:

```
affinity(w) = (author_count(w) / author_total_tokens)
            / (corpus_count(w) / corpus_total_tokens)
```

Sanity-check (back-solving C/A from screenshot rows):

| word       | aᶜ   | сᶜ    | aff    | implied C/A |
|------------|-----:|------:|-------:|------------:|
| challenger |  385 |  2245 | 122.21 |      712.65 |
| knolles    |   92 |   553 | 118.56 |      712.65 |
| holmes     | 4045 | 27051 | 106.56 |      712.61 |

All consistent. Author tokens ≈ 2.8B / 712.6 ≈ 3.93M — realistic for
~70 books of Doyle in PG. **Formula correct, numbers correctly
displayed.** The problem was never the math.

## What's in the diff

```
scripts/v2/planner/entities.py             +50  (Beowulf/Paradise Lost/Milton + country word-bound)
scripts/v2/planner/plan.py                 +62  (polysemy scope widen + etymology-ratio recipe)
scripts/v2/tools/authors/affinity.py       +21  (surname filter wired)
scripts/v2/tools/authors/_surname_filter.py +175 (new module)
scripts/v2/tools/books/affinity_book.py    +14  (surname filter wired)
tests/v2/test_polysemy_and_etymology_ratio.py +160 (16 tests)
tests/v2/test_affinity_surname_filter.py      +175 (9 tests)
```

## Tests

```
tests/v2/test_polysemy_and_etymology_ratio.py
  PolysemyScopeFix × 3
  BeowulfParadiseLostMiltonAliases × 5
  CountryAliasNoEtymologyFalsePositive × 4
  EtymologyRatioSmartClarify × 4

tests/v2/test_affinity_surname_filter.py
  SurnameBlocklistPrimitives × 6
  AffinityByAuthorIntegration × 2
  AffinityByBookIntegration × 1
```

Combined: **637 / 0** (was 612 / 0 at v3.1).

## Deploy

```bash
sudo -u claude git -C /home/claude/wordcracker pull
sudo systemctl restart wordcracker-chat
# no admin or chroma changes — chat restart is enough
```

The surname filter reads PG metadata lazily on first call after
restart. First "фирменные слова X" query takes ~150ms extra for the
CSV parse + dedup; subsequent calls hit the mtime cache.

## What's still NOT done

- **Token-coverage etymology ratio** — real Latinate-vs-Germanic
  percentages need spaCy POS-tag + per-token Wiktionary lookup over
  the whole text. Recipe instead. Sprint 20.
- **Polysemy with explicit scope phrase** — «все значения слова set
  в Paradise Lost» works; «все значения слова light» (no scope) falls
  to clarify by design. Could default to `{'author':'.*'}` here too,
  but the global polysemy view across 55k books is rarely what users
  actually want.
- **`burger` / `hastie` / `flannigan` long-tail** — if any obscure
  Doyle/Conrad/Hardy character names still leak through the curated
  set, add them to `_CURATED_CHARACTER_SURNAMES`. The PG-metadata
  layer catches surnames that happen to be PG author surnames.

Co-developed with Claude Opus 4.7 (1M context).
