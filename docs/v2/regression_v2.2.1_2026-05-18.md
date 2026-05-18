---
project: wordcracker
type: regression-report
release: v2.2.1
date: 2026-05-18
host: Windows (Stan's dev box) — without SOW deploy access
---

# Regression Report — v2.2.1 (pre-deploy)

**TL;DR:** local regression at all layers I can reach without SOW SSH is
clean. Audit caught one real bug Stan didn't see (NAME_AFTER_KEY guard
was disabled by `re.IGNORECASE`) — hotfix shipped as v2.2.1 before
deploy. **Functional 40/40 end-to-end on SOW still pending** because
SSH from this Windows host to the SOW box was blocked by auto-mode
classifier and the manual one-liner Stan ran earlier failed to find
the right repo path (`/home/claude/wordcracker`, not `~/wordcracker`).

## What ran

| Layer | Result | Where |
|---|---|---|
| Unit tests | **191/191 pass** (40 subtests) | `python -m pytest tests/v2/` |
| AST parse all `scripts/*.py` | **78/78 syntactically valid** | |
| Import sanity of v2 modules | **13/13 import without errors** | |
| Tool registry populated | **35/35 tools registered** (same as v2.0.7 baseline) | |
| Intent classify + plan build dry-run on all 40 vault Q's | **32 tool-driven / 2 clarify / 6 OOS** (= v2.1 baseline + Q40 upgrade) | `C:/Users/Standa/dryrun_40.txt` |
| Historical-bug regression probes | **10/10 PASS** | inline script |
| Side-effect audit on new v2.2 code | found 1 real bug → fixed in v2.2.1 | inline script |

## Detailed findings

### 1. Unit suite — 191/191

Full pytest run finishes in ~2 seconds. Breakdown of new tests added
during this Sprint 11 round:

- `test_intent.py::test_pohozhi_na_AI_not_author_closest` — Bug A from
  «Отловленные баги»
- `test_entities.py::test_name_after_imya` — Bug B from «Отловленные баги»
- `test_entities.py::test_name_after_imya_filler_negative` — caught
  during audit (v2.2.1)
- `test_plan.py::test_composite_compare_q40` — Sprint 11.4 plan shape
- `test_router.py::test_chained_steps_with_author_regex_injection` —
  Sprint 11.4 router inject

### 2. Intent / plan dry-run, all 40 vault questions

Verified each question still classifies + plans cleanly without
dispatching tools. Numbers identical to v2.1 baseline except Q40 (which
is the whole point of Sprint 11.4).

| Result | Count | Q IDs |
|---|---|---|
| tool-driven | 32 | 1-9, 11-19, 21-24, 26-28, 30-36, 40 |
| clarify | 2 | Q10 («второго уровня» without author/book), Q36 («больше всего редких прилагательных» without author) |
| out_of_scope | 6 | Q20, Q29 (translation_quality — RU↔EN corpus); Q25 (genre_compare); Q37 (word_dialogue); Q38 (period_vocab + gender); Q39 (word_movement без scope) |

Notable per-question verdicts:

- **Q05** (Bleak House vs Huckleberry Finn) — `book_compare` ✓ (was the
  v2.0.7 fix, still holding)
- **Q15** (По vs Lovecraft при ", The Raven" / "At the Mountains") —
  routes to `word_emotion` (от слов «мрачный» в этом запросе нет, но
  «emotion_collocates» — это для слов страха/ужаса, что точно к Лавкрафту
  применимо). Не идеально, но v2.1 baseline.
- **Q27** (Мелвилл, Конрад, Стивенсон) — `author_vocab` с 3 шагами
  `affinity_by_author` (Sprint 11.3 multi-author fan-out, работает)
- **Q40** — `composite_compare` с 4 шагами (Sprint 11.4 ✓)

### 3. 10 caught-bug regression probes

All historical caught bugs still hold their fix:

| # | Bug | Probe outcome |
|---|---|---|
| B1 | PG1327 hallucination on Crime/Punishment | book_vocab → PG2554 ✓ |
| B2a | Свифт alias missing | extract → ^Swift, ✓ |
| B2b | Хемингуэя падежная форма | extract → ^Hemingway, ✓ |
| B3 | LOTR copyright cryptic fail | `_copyright_refusal_if_book_under_copyright` → OOS ✓ |
| B4 | Multi-turn follow-up «приведи примеры» | `infer_followup_intent` → word_contexts ✓ |
| B5 | Q5 routing → character names | book_compare ✓ |
| B6 | Etymology multi-family «latin/french» | family=latin ✓ |
| B7 | Critic over-flag noise | MAX_FLAGS=2 + 3-intent skip list ✓ |
| B8 | v2.2 «похожи на ИИ» false-match | classify → clarify (NOT author_closest) ✓ |
| B9 | v2.2 «имени Анна» word extract | extract.word = «анна» ✓ |

### 4. Side-effect audit — caught 1 real bug

Each v2.2 change was probed for collateral damage:

**Change A — `composite_compare` intent (priority 145):** doesn't steal
Q12/Q23 from `country_compare` (priority 135). Q40 lands in
composite_compare ✓.

**Change B — `author_closest` tightening:** 5 positive probes (Doyle,
Уайльд, ближе, closest, автор-детективов) all classify correctly. 3
negative probes («похож на правду / ИИ / сказку») now return clarify
instead of author_closest ✓.

**Change C — `_NAME_AFTER_KEY`:** ❌ FAILED on 2 of 7 probes. «имя
автора» extracted word="автора", «от моего имени напиши» extracted
word="напиши". Root cause: the regex was compiled with `re.IGNORECASE`,
which made the `[A-ZА-ЯЁ]` proper-noun lead-letter guard impotent.
**Fixed in v2.2.1** — dropped `re.IGNORECASE`, spelled the keyword stem
`[Ии]м(?:я|ени|енем)` so it's still case-flexible on the trigger but
the captured-word class is now strictly case-sensitive on the first
letter.

**Change D — Router `_inject` with `author_regex` mode:** all 4
spot-checks pass (pg_id, author_regex, scope, no-op on empty data) ✓.

### 5. Tool registry

Still 35 tools, identical to v2.0.7 / v2.1 baseline. No tool dropped, no
new tool added in Sprint 11.4 / 11.5.

## What's NOT covered by this report

These layers need the SOW to actually answer 40 questions through the
real LLM + corpus:

- Functional 40-question suite via `/api/chat/stream`
- Critic noise rate (target <5/32 — v2.1 hit 9/32, hope v2.2 stays
  there or better)
- Wall-clock distribution + heavy-tool p95
- composite_compare plan actually returning sensible affinity data for
  the GB/US leaders
- Stats footer rendering correctly in the browser
- Retry-with-scope button click behavior

These I cannot run from Windows without SSH to SOW or working
authentication to the public `slovoeb.net` endpoint.

## Recommended deploy command (to run on SOW)

```bash
sudo -u claude git -C /home/claude/wordcracker pull
sudo systemctl restart wordcracker-chat
sleep 5 && curl -sf http://127.0.0.1:8890/health && echo "chat up"
sudo -u claude python3 /home/claude/wordcracker/tests/v2/run_functional_40.py \
  --base-url http://127.0.0.1:8890 --engine v2 \
  --out "/home/claude/wordcracker/docs/v2/test_report_v2.2.1_$(date +%F).md"
```

## Releases live

- v2.2 (Sprint 11.4 + 11.5 + 2 caught bugs) —
  https://github.com/Standaoerby/wordcracker/releases/tag/v2.2
- v2.2.1 (NAME_AFTER_KEY hotfix from this audit) —
  https://github.com/Standaoerby/wordcracker/releases/tag/v2.2.1

## Commits this round

```
8cfbdc7 docs(v2): v2.2.1 hotfix release body
c7d445d fix(v2): _NAME_AFTER_KEY proper-noun guard was effectively disabled
fd793d9 docs(v2): v2.2 release body — add bugfix section + bump test count
9e7a3da fix(v2): 2 caught bugs — author_closest false-match + name extraction
b3ac8c1 docs(v2): v2.2 release body draft
f8a9de2 feat(chat): Sprint 11.5 — stats footer + retry-with-scope button
5940523 feat(v2): Sprint 11.4 — composite_compare intent for Q40
```
