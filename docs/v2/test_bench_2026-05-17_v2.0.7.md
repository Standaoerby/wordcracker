# v2 performance benchmark — 2026-05-17T23:01:46

- runs: 1
- questions per run: 40
- total calls: 40
- cache hit rate: 0% (0/34)
- critic: 9 clean / 23 flagged

## Per-intent wall-clock latency

| Intent | n | p50 (s) | p95 (s) | max (s) | mean (s) |
|---|---:|---:|---:|---:|---:|
| author_closest | 1 | 10.16 | 10.16 | 10.16 | 10.16 |
| author_vocab | 4 | 17.58 | 17.58 | 20.59 | 13.46 |
| book_archaic | 3 | 8.62 | 8.62 | 9.72 | 8.67 |
| book_compare | 2 | 17.98 | 13.09 | 17.98 | 15.54 |
| book_vocab | 2 | 17.36 | 16.08 | 17.36 | 16.72 |
| country_compare | 3 | 14.10 | 14.10 | 20.71 | 14.22 |
| country_vocab | 1 | 15.55 | 15.55 | 15.55 | 15.55 |
| introduction | 1 | 0.02 | 0.02 | 0.02 | 0.02 |
| learning | 4 | 23.13 | 23.13 | 23.27 | 16.61 |
| lexical_wealth | 1 | 10.83 | 10.83 | 10.83 | 10.83 |
| out_of_scope | 6 | 0.00 | 0.00 | 0.00 | 0.00 |
| topic_words | 1 | 9.43 | 9.43 | 9.43 | 9.43 |
| vocab_passport | 1 | 19.92 | 19.92 | 19.92 | 19.92 |
| word_collocates | 1 | 11.19 | 11.19 | 11.19 | 11.19 |
| word_contexts | 1 | 9.03 | 9.03 | 9.03 | 9.03 |
| word_emotion | 3 | 13.28 | 13.28 | 13.65 | 12.15 |
| word_etymology | 1 | 15.46 | 15.46 | 15.46 | 15.46 |
| word_pos | 1 | 4.05 | 4.05 | 4.05 | 4.05 |
| word_timeline | 3 | 15.05 | 15.05 | 15.51 | 15.05 |

## Per-tool internal runtime

| Tool | n | p50 (ms) | p95 (ms) | mean (ms) |
|---|---:|---:|---:|---:|
| affinity_by_author | 4 | 458 | 458 | 480 |
| affinity_by_book | 4 | 10662 | 10662 | 7797 |
| author_influences | 1 | 28 | 28 | 28 |
| author_profile | 1 | 4020 | 4020 | 4020 |
| book_archaic_words | 3 | 6 | 6 | 6 |
| emotion_collocates | 3 | 100 | 100 | 99 |
| find_words_by_etymology | 1 | 39340 | 39340 | 39340 |
| learning_words | 3 | 6457 | 6457 | 6047 |
| top_authors_by | 1 | 59742 | 59742 | 59742 |
| top_authors_by_country | 6 | 68 | 68 | 46 |
| word_collocates | 2 | 37033 | 878 | 18955 |
| word_contexts | 1 | 210 | 210 | 210 |
| word_pos_distribution | 1 | 510 | 510 | 510 |
| words_disappearing_after | 3 | 25857 | 25857 | 25181 |