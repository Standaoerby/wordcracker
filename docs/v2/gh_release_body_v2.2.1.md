# wordcracker v2.2.1 — `_NAME_AFTER_KEY` hotfix

Patch on top of v2.2. Caught during the full regression audit before
deploy: the new `_NAME_AFTER_KEY` regex from v2.2's «имени Анна» bug fix
had `re.IGNORECASE` set, which inadvertently disabled the proper-noun
guard on the captured word.

## Symptom

- «имя автора» → `e.word = "автора"` (should be `None`)
- «от моего имени напиши письмо» → `e.word = "напиши"` (should be `None`)

In production this would have leaked filler words into `word_contexts` /
`hybrid_search` calls and produced confusing responses on what should be
clarify-out.

## Fix

Drop `re.IGNORECASE`; spell the keyword stem `[Ии]м(?:я|ени|енем)` by
hand so it stays case-flexible on the keyword while the
`[A-ZА-ЯЁ][a-zA-Zа-яё-]{1,29}` capture class is now strictly
case-sensitive on the lead letter — the proper-noun guard actually
fires.

Added 3 negative probes in `test_name_after_imya_filler_negative`
covering the three filler phrasings the audit caught.

## Tests

- Unit: 191/191 (+1 over v2.2)
- All 10 historical caught-bug regression probes still PASS
- 40-question dry-run unchanged: 32 tool-driven / 2 clarify / 6 OOS

Co-developed with Claude Opus 4.7 (1M context).
