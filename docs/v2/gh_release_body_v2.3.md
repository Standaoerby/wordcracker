# wordcracker v2.3 — adversarial hardening + routing fixes

The release tonight has three parts:

1. **5 routing bugs caught in dry-run audit** (passing functional 40/40 was hiding wrong-intent classifications)
2. **Adversarial hardening** ahead of an upcoming red-team probe
3. **Refactor** — copyright-refusal decorator deduplicates 8 plan builders

## Routing bugs

| Q | Symptom | Root cause | Fix |
|---|---|---|---|
| Q10 | «лексику "второго уровня" из "Pride and Prejudice"» — clarify-out, book never resolved | First-match book_title win — quoted RU scope phrase beat the EN book title | Skip Cyrillic-only quoted phrases <25 chars unless in KNOWN_BOOKS |
| Q15 | «стиля Лавкрафта в "At the Mountains of Madness"» → routed to `word_emotion` | Bare `terror\|madness` matched the book-title word | Drop the bare alternation; require «слов* + emotion word» or «рядом со словами …» |
| Q21 | «300 слов» silently became 30 in plan, user never told | top_n capped in `_plan_learning`, no surface to LLM | Pass `_capped_from` to wrapper, emit `ToolWarning("top_capped")` for the renderer |
| Q30 | «чтобы НЕ было слишком много архаизмов» → `book_archaic` (treat negation as positive) | Bare «архаизм*» triggered, no negation guard | Anchor book_archaic to positive context; add «произведения … можно читать» trigger to book_recommendation |
| meta-corpus | «что у тебя с копирайтом?» → clarify | corpus_meta regex didn't cover «что у тебя с …» / «расскажи про охват» phrasings | Add 4 new patterns for coverage / copyright / language / period meta-questions |

## Adversarial hardening

| Layer | Cap | Why |
|---|---|---|
| Content-Length | 64 KB | Reject multi-MB payloads before parsing — no unbounded `rfile.read()` |
| question chars | 4 000 | ~5× the longest legitimate Q40 from the vault |
| history turns | 50 | Past 50 turns = ~1 hour of real conversation |
| history total bytes | 64 KB | Tail-clipped; 10 MB context bomb caps at 64 KB |
| control chars | stripped (except `\n\t`) | Stops downstream regex sabotage |

**Prompt-injection intent guards** — 10 patterns route to `out_of_scope` at the planner level, never reach the LLM:

- «забудь предыдущие инструкции / ignore previous instructions / forget all instructions»
- «reveal your system prompt / покажи system prompt»
- «ты теперь … / you are now … / pretend to be … / твоя новая роль»

The wordcracker:v2 Modelfile SYSTEM prompt is still the last line of defense, but bouncing at the planner saves an LLM round-trip and produces a deterministic refusal that's easy to audit in logs.

## Refactor

`_copyright_refusal_if_book_under_copyright(e); if refusal: return refusal` was repeated 8 times across plan builders. Extracted into a `@_with_copyright_check` decorator. Net −24 lines and a single place to evolve the copyright-refusal policy.

## Tests

- **Unit: 198/198** (+7 over v2.2.2 — Q10 Cyrillic scope, Q15 madness title, Q30 archaism negation, meta-corpus probes, 10× injection guards)
- **40-question dry-run: 33 tool-driven / 1 clarify / 6 OOS** (was 32/2/6 on v2.2.2 — Q30 now reachable as book_recommendation)
- **No regressions** on Q05, Q08, Q11, Q18, Q35 (positive-context emotion / archaic queries unchanged)

## Coming next

Adversarial probe — Stan runs a red-team session against the deployed v2.3 chat. Whatever bugs come out of that → v2.3.1+ patches.

Co-developed with Claude Opus 4.7 (1M context).
