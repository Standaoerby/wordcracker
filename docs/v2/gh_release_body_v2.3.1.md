# wordcracker v2.3.1 — 3 production bugs from Stan's round 2

Three real bugs Stan caught in the chat after v2.3 deploy. Each one had been hiding behind a green 40-question functional test — they only surface on specific user flows.

## Bug A — affinity leaks character names for Russian authors

«дай фирменные слова пушкина» returned a top table of *character / place transliterations* — gavril 5445, lisaveta 4022, korsakoff 4002, simbirsk, **pushkin himself**, kibitka, mossoo, beaupre, marya, ivan, petrovitch, boyars, vladimir, andrei. Not stylistic markers, just OCR-mangled proper nouns from English translations of Капитанская дочка, Пиковая дама, etc.

Root cause: `_auto_min_corpus_count` returned **100** by default — too soft for transliterated proper nouns (corpus_count 200-800). spaCy POS tags «Beaupré» / «Simbirsk» inconsistently so the PROPN-drop pass didn't catch them.

Fix:
- Default floor 100 → **500** (matches POS/country case).
- Russian-author allowlist (Pushkin, Tolstoy, Dostoyevsky, Chekhov, Turgenev, Gogol, Lermontov, Bulgakov) → **1500**. Aggressive corpus_count cut nukes transliterations regardless of POS.

## Bug B — «отсортируй их по количеству упоминаний» → clarify

After the table above, follow-up «отсортируй …» got `intent: clarify` — context lost.

Root cause: `_REF_TRIGGERS` didn't list re-rank phrases, and `infer_followup_intent` had no mapping for them anyway.

Fix:
- Trigger regex extended: `отсортируй / сортируй / пересортируй / перегруппируй / перестрой / в другом виде / по убыванию / по возрастанию / sort them by / re-rank`.
- New `_RERANK_PATTERNS`. When matched, `infer_followup_intent(text, history)` re-classifies the most recent user message and returns its intent. The plan re-runs (tool result is a cache hit) and the LLM re-renders sorted.
- `rag_v2.py` now passes `history` into both `infer_followup_intent` call sites.

## Bug C — copyright refusal is too curt

«фирменные слова из "The Lord of the Rings"» / «1984» / etc. used to refuse with just «отсутствует в корпусе». Stan's note: «всё, что косается копирайченных текстов, он должен говорить, что либо они есть в базе (ограниченно), либо по ним доступна только мета-информация».

Refusal text restructured into three explicit parts:

1. **WHY** no full text — copyright window, US ~1929 / UK ~1973
2. **WHAT IS available** — metadata via Gutendex (title, author, year, downloads). No tokenized text → no stylometry / affinity / contexts.
3. **WHICH public-domain analog to read instead** — per-book mapping:
   - LOTR → Morris «The Well at the World's End» (PG169) — fantasy archaism, direct influence on Tolkien
   - Hobbit → Morris «The House of the Wolfings» (PG2885) — Germanic/Norse roots
   - 1984 → Conrad «Heart of Darkness» (PG219) — dystopian darkness, B2+
   - Old Man and the Sea → Twain «Huckleberry Finn» (PG76) — laconic American style

Also: leading-«the» fuzzy match. «Old Man and the Sea» (no article) now resolves to KNOWN_BOOKS key «the old man and the sea». Users routinely drop the leading article when typing.

## Tests

- **Unit: 206/206** (+8 over v2.3 — 3× re-rank followup, 2× copyright refusal, 3× high-translit corpus floor)
- **40-question dry-run unchanged**: 33/1/6 (no positive regression)
- Bug A regression: probe-test verifies Pushkin/Tolstoy → floor 1500, Wodehouse → floor 500
- Bug B regression: re-rank synonyms tested across 3 phrasings + no-history branch
- Bug C regression: LOTR + Old-Man-without-the both produce structured OOS with analog

Co-developed with Claude Opus 4.7 (1M context).
